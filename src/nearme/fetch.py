"""HTTP layer for Caltrans QuickMap.

QuickMap is a React SPA; its data is KML files listed in config/layers.json.
There is no documented public API, so we behave like a polite client:
single cached copy per file, conditional GET (ETag / Last-Modified served by
the origin), a real User-Agent, and a minimum refresh interval matching the
site's own updateIntervalMinutes (3). Falls back to the cached copy on network
failure so "near me" still answers offline.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .kml import parse_kml
from .model import Event

BASE = "https://quickmap.dot.ca.gov"
LAYERS_CONFIG = f"{BASE}/config/layers.json"
USER_AGENT = "nearme/0.1 (personal traffic checker; contact via local use)"
MIN_REFRESH_SECONDS = 180


@dataclass
class LayerDef:
    id: str
    name: str
    files: list[str]
    source: str
    geometry: str


@dataclass
class FetchResult:
    events: list[Event]
    layer: str
    stale: bool = False
    from_cache: bool = False
    note: str = ""


@dataclass
class Fetcher:
    cache_dir: str = ".cache"
    min_refresh_seconds: int = MIN_REFRESH_SECONDS
    _layers: dict[str, LayerDef] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)

    # ------------------------------------------------------------------ http
    def _request(self, url: str, headers: dict[str, str]) -> tuple[int, dict, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, dict(r.headers), r.read()

    def get_text(self, url: str, cache_name: str, force: bool = False) -> tuple[str, FetchResult]:
        """Conditional GET. Returns (body, meta). Never raises on network error."""
        body_path = os.path.join(self.cache_dir, cache_name)
        meta_path = body_path + ".meta"
        meta: dict = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                meta = {}

        if not force and meta and time.time() - meta.get("fetched_at", 0) < self.min_refresh_seconds:
            if os.path.exists(body_path):
                with open(body_path, encoding="utf-8", errors="replace") as f:
                    return f.read(), FetchResult([], "", from_cache=True, note="within TTL")

        headers: dict[str, str] = {}
        if meta.get("etag") and os.path.exists(body_path):
            headers["If-None-Match"] = meta["etag"]
        elif meta.get("last_modified") and os.path.exists(body_path):
            headers["If-Modified-Since"] = meta["last_modified"]

        try:
            status, hdrs, body = self._request(url, headers)
        except urllib.error.HTTPError as e:
            if e.code == 304 and os.path.exists(body_path):
                meta["fetched_at"] = time.time()
                self._write_meta(meta_path, meta)
                with open(body_path, encoding="utf-8", errors="replace") as f:
                    return f.read(), FetchResult([], "", from_cache=True, note="304 not modified")
            if os.path.exists(body_path):
                with open(body_path, encoding="utf-8", errors="replace") as f:
                    return (
                        f.read(),
                        FetchResult([], "", stale=True, note=f"http {e.code}, using cache"),
                    )
            raise RuntimeError(f"{url}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if os.path.exists(body_path):
                with open(body_path, encoding="utf-8", errors="replace") as f:
                    return f.read(), FetchResult([], "", stale=True, note=f"offline: {e}")
            raise RuntimeError(f"{url}: {e}") from e

        text = body.decode("utf-8", errors="replace")
        with open(body_path, "w", encoding="utf-8") as f:
            f.write(text)
        meta = {
            "fetched_at": time.time(),
            "etag": hdrs.get("ETag"),
            "last_modified": hdrs.get("Last-Modified"),
            "url": url,
        }
        self._write_meta(meta_path, meta)
        return text, FetchResult([], "", note="fresh")

    @staticmethod
    def _write_meta(path: str, meta: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f)

    # ---------------------------------------------------------------- layers
    def layers(self, force: bool = False) -> dict[str, LayerDef]:
        if self._layers and not force:
            return self._layers
        text, _ = self.get_text(LAYERS_CONFIG, "layers.json", force=force)
        defs: dict[str, LayerDef] = {}
        for item in json.loads(text):
            kml = item.get("kml")
            if not kml:
                continue
            if isinstance(kml, str):
                kml = [kml]
            defs[item["id"]] = LayerDef(
                id=item["id"],
                name=item.get("name", item["id"]),
                files=kml,
                source=_source_for(item["id"], kml),
                geometry=item.get("geometry", ""),
            )
        self._layers = defs
        return defs

    def fetch_layer(self, layer_id: str, force: bool = False) -> FetchResult:
        defs = self.layers()
        if layer_id not in defs:
            raise KeyError(f"unknown layer {layer_id!r}; try: {', '.join(sorted(defs))}")
        d = defs[layer_id]
        events: list[Event] = []
        stale = False
        notes: list[str] = []
        for path in d.files:
            url = BASE + path
            name = path.rsplit("/", 1)[-1]
            text, meta = self.get_text(url, name, force=force)
            stale = stale or meta.stale
            notes.append(f"{name}: {meta.note}")
            events.extend(parse_kml(text, d.source, layer_id))
        return FetchResult(events, layer_id, stale=stale, note="; ".join(notes))


def _source_for(layer_id: str, files: list[str]) -> str:
    joined = " ".join(files).lower()
    if "chp" in joined or "chp" in layer_id:
        return "chp"
    return "caltrans"
