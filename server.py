#!/usr/bin/env python3
"""Local web app: map of CHP incidents + Caltrans lane closures.

Stdlib only. Serves web/ as static files and a small GeoJSON API. One process,
one cache, one upstream fetch loop - the browser never touches QuickMap, and
N polling tabs still produce at most one conditional GET per upstream file per
refresh interval.

    python server.py            # http://127.0.0.1:8388
    python server.py --port 9000 --layers chp_incidents,lane_closures,chain_controls
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from nearme.api import LEVEL_COLORS, events_as_geojson, feed_status, load_events  # noqa: E402
from nearme.fetch import Fetcher  # noqa: E402
from nearme.geocode import Geocoder, GeocodeError  # noqa: E402
from nearme.query import DEFAULT_LAYERS  # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class State:
    """Parsed events, refreshed on a timer, guarded by a lock."""

    def __init__(self, fetcher: Fetcher, layers, refresh_seconds: int) -> None:
        self.fetcher = fetcher
        self.layers = layers
        self.refresh_seconds = refresh_seconds
        self.lock = threading.Lock()
        self.events: list = []
        self.statuses: list[str] = []
        self.updated_at = 0.0
        self.error: str | None = None

    def refresh(self, force: bool = False) -> bool:
        if not force and time.time() - self.updated_at < min(self.refresh_seconds, 15):
            return False
        try:
            with self.lock:
                events, statuses = load_events(self.fetcher, self.layers, force=force)
                self.events, self.statuses = events, statuses
                self.updated_at = time.time()
                self.error = None
            return True
        except Exception as e:  # never kill the timer thread on a bad fetch
            self.error = f"{type(e).__name__}: {e}"
            return False

    def loop(self) -> None:
        while True:
            self.refresh()
            time.sleep(self.refresh_seconds)

    def snapshot(self):
        with self.lock:
            return list(self.events)


class Handler(BaseHTTPRequestHandler):
    state: State = None  # type: ignore[assignment]
    geocoder: Geocoder = None  # type: ignore[assignment]
    server_version = "nearme/0.1"

    # ------------------------------------------------------------------ util
    def log_message(self, fmt, *args) -> None:
        sys.stderr.write(
            f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n"
        )

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), TYPES[".json"])

    # ------------------------------------------------------------------ GET
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            try:
                return self._api(path, parse_qs(parsed.query))
            except Exception as e:  # keep the UI alive on a bad request
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._static(path)

    def _api(self, path: str, qs: dict) -> None:
        st = self.state
        if path == "/api/events":
            bbox = None
            if "bbox" in qs:
                try:
                    lon_min, lat_min, lon_max, lat_max = (float(v) for v in qs["bbox"][0].split(","))
                    bbox = (lon_min, lat_min, lon_max, lat_max)
                except ValueError:
                    return self._json({"error": "bbox must be lon_min,lat_min,lon_max,lat_max"}, 400)
            center = None
            if "lat" in qs and "lon" in qs:
                try:
                    center = (float(qs["lat"][0]), float(qs["lon"][0]))
                except ValueError:
                    return self._json({"error": "lat/lon must be numbers"}, 400)
            levels = qs["levels"][0].split(",") if "levels" in qs else None
            within = float(qs["within"][0]) if "within" in qs else None
            q = qs.get("q", [""])[0].strip() or None
            fc = events_as_geojson(
                st.snapshot(), bbox=bbox, center=center, levels=levels, q=q, within=within
            )
            fc["nearme"] = {
                "updated_at": st.updated_at,
                "total": len(st.events),
                "returned": len(fc["features"]),
                "statuses": st.statuses,
                "error": st.error,
                "levels": LEVEL_COLORS,
            }
            return self._json(fc)

        if path == "/api/geocode":
            query = qs.get("q", [""])[0].strip()
            if not query:
                return self._json({"error": "q required"}, 400)
            try:
                point, label = self.geocoder.resolve(query)
            except GeocodeError as e:
                return self._json({"error": str(e)}, 422)
            return self._json({"lat": point[0], "lon": point[1], "label": label})

        if path == "/api/meta":
            return self._json(
                {
                    "layers": sorted(st.layers),
                    "all_layers": sorted(st.fetcher.layers().keys()),
                    "counts": {"events": len(st.events)},
                    "updated_at": st.updated_at,
                    "refresh_seconds": st.refresh_seconds,
                    "feeds": feed_status(st.fetcher),
                    "statuses": st.statuses,
                    "error": st.error,
                    "levels": LEVEL_COLORS,
                    "attribution": "Incidents courtesy CHP; lane closures courtesy Caltrans, via quickmap.dot.ca.gov",
                }
            )

        if path == "/api/refresh":
            return self._json({"refreshed": st.refresh(force=True), "error": st.error})

        return self._json({"error": "not found", "path": path}, 404)

    def _static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        target = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not target.startswith(WEB + os.sep) or not os.path.isfile(target):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        with open(target, "rb") as f:
            body = f.read()
        ctype = TYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        cache = "no-cache" if target.endswith("index.html") else "max-age=3600"
        self._send(200, body, ctype, cache)


def main() -> int:
    ap = argparse.ArgumentParser(description="nearme web app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8388)
    ap.add_argument("--layers", default=",".join(DEFAULT_LAYERS))
    ap.add_argument("--cache-dir", default=".cache")
    ap.add_argument("--refresh", type=int, default=90, help="seconds between upstream refreshes")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    cfg = {}
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)

    fetcher = Fetcher(cache_dir=args.cache_dir, min_refresh_seconds=min(args.refresh, 60))
    state = State(fetcher, tuple(args.layers.split(",")), args.refresh)
    Handler.state = state
    Handler.geocoder = Geocoder(cache_dir=args.cache_dir, places=cfg.get("places", {}))

    print("priming feed (one fetch of the statewide KML)...")
    state.refresh(force=True)
    print(f"  {len(state.events)} events: {'; '.join(state.statuses)}")
    threading.Thread(target=state.loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"nearme on http://{args.host}:{args.port}  (ctrl-c to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
