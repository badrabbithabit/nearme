"""Resolve "near me" into a lat/lon.

Accepts coordinates, a US ZIP, a place name, or a named place from config.
Geocoding goes to OpenStreetMap Nominatim (no API key) and is cached, because
the same query tends to be repeated every run.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "nearme/0.1 (personal traffic checker)"


class GeocodeError(Exception):
    pass


_COORD_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


def parse_point(text: str) -> tuple[float, float] | None:
    m = _COORD_RE.match(text)
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise GeocodeError(f"{text!r} looks like coordinates but is out of range")
    return lat, lon


class Geocoder:
    def __init__(self, cache_dir: str = ".cache", places: dict[str, str] | None = None) -> None:
        self.cache_dir = cache_dir
        self.places = places or {}
        self.path = os.path.join(cache_dir, "geocode.json")
        os.makedirs(cache_dir, exist_ok=True)
        self._cache: dict[str, list[float]] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._cache = {}

    def resolve(self, query: str) -> tuple[tuple[float, float], str]:
        """Return ((lat, lon), label)."""
        q = query.strip()
        pt = parse_point(q)
        if pt:
            return pt, f"{pt[0]:.4f}, {pt[1]:.4f}"

        named = self.places.get(q.lower())
        if named:
            pt = parse_point(named)
            if pt:
                return pt, f"{q} ({pt[0]:.4f}, {pt[1]:.4f})"
            q = named

        key = q.lower()
        if key in self._cache:
            lat, lon = self._cache[key]
            return (lat, lon), q

        params = {
            "q": q,
            "format": "jsonv2",
            "limit": "1",
            "addressdetails": "0",
        }
        if _ZIP_RE.match(q):
            params["countrycodes"] = "us"
        url = f"{NOMINATIM}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
        except Exception as e:  # network or JSON
            raise GeocodeError(f"could not geocode {q!r}: {e}") from e
        if not data:
            raise GeocodeError(f"no geocode result for {q!r}")
        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
        self._cache[key] = [lat, lon]
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=1)
        return (lat, lon), data[0].get("display_name", q)
