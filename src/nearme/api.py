"""GeoJSON API layer shared by the web app and any future client.

Design notes:
  * The browser never talks to QuickMap. One server-side Fetcher holds the only
    cache, so N browsers polling does not multiply requests to a state agency.
  * Geometry: a closure's point is the Feature geometry; its pavement trace (the
    LCS polyline) rides along in properties.trace so the client can draw both.
  * Properties carry the trust tiering explicitly: `authoritative` (Caltrans LCS
    record) vs inferred-from-CAD-text, plus `evidence` quotes so the UI can show
    what a label was guessed from.
"""

from __future__ import annotations

import json
import math
import os
import time

from .fetch import Fetcher
from .geo import distance_mi, polyline_len_mi
from .query import DEFAULT_LAYERS, classify_all, gather

LEVEL_COLORS = {
    "full": "#d21f26",
    "one_way": "#e2711d",
    "lanes": "#f0a92e",
    "shoulder": "#4f86c4",
    "blocked": "#8a8f98",
    "none": "#b9bec7",
}


def load_events(fetcher: Fetcher, layers=DEFAULT_LAYERS, force: bool = False):
    """Fetch + parse + classify. Returns (events, statuses).

    classify_all() is not optional here: without it every event would arrive at
    the browser with level "none" and no direction/routes, silently disabling the
    colour coding and the level filter.
    """
    events, statuses = gather(fetcher, layers, force=force)
    classify_all(events)
    return events, statuses


def _lonlat(pt):
    return [pt[1], pt[0]]


def to_feature(e, center=None) -> dict:
    level = e.closure.level if e.closure else "none"
    geom_type, coords = e.geometry
    geometry = {
        "type": "Point" if geom_type == "point" else "LineString",
        "coordinates": _lonlat(coords) if geom_type == "point" else [_lonlat(c) for c in coords],
    }
    trace = e.path or (coords if geom_type == "line" else None)
    props = {
        "uid": e.event_id or e.closure_id or f"{e.source}:{id(e) % 100000}",
        "ids": e.ids or ([e.event_id] if e.event_id else []),
        "source": e.source,
        "layer": e.layer,
        "kind": e.kind or e.title or "(untitled)",
        "title": e.title or "",
        "level": level,
        "color": LEVEL_COLORS.get(level, "#b9bec7"),
        "authoritative": bool(e.closure and e.closure.authoritative),
        "lanes_closed": e.lanes_closed,
        "lanes_total": e.lanes_total,
        "lanes_affected": e.closure.lanes_affected if e.closure else None,
        "direction": e.closure.direction if e.closure else None,
        "evidence": (e.closure.evidence if e.closure else [])[:4],
        "reported_by": e.reported_by,
        "location": e.location_text,
        "reason": e.reason,
        "routes": e.routes,
        "closure_id": e.closure_id,
        "occurred": e.occurred.isoformat(timespec="minutes") if e.occurred else None,
        "updated": e.updated.isoformat(timespec="minutes") if e.updated else None,
        "expected_end": e.expected_end.isoformat(timespec="minutes") if e.expected_end else None,
        "notes": [
            {
                "when": n.when.isoformat(timespec="minutes") if n.when else None,
                "unit": n.unit,
                "tag": n.tag,
                "text": n.text,
            }
            for n in (e.notes or [])[-14:]
        ],
        "trace_mi": round(polyline_len_mi(e.path), 2) if e.path else None,
        "trace": [_lonlat(p) for p in trace] if trace and len(trace) > 1 else None,
    }
    if center is not None:
        d = distance_mi(geom_type, coords, center)
        if e.path:
            d = min(d, distance_mi("line", e.path, center))
        props["distance_mi"] = round(d, 2)
    return {"type": "Feature", "geometry": geometry, "properties": props}


def in_bbox(e, lon_min, lat_min, lon_max, lat_max) -> bool:
    pad = 0.04  # a closure whose trace crosses the edge should still draw
    pts = e.path or []
    if e.geometry[0] == "line":
        pts = pts + list(e.geometry[1])
    else:
        pts = pts + [e.geometry[1]]
    return any(
        lon_min - pad <= lon <= lon_max + pad and lat_min - pad <= lat <= lat_max + pad
        for lat, lon in pts
    )


def events_as_geojson(events, bbox=None, center=None, levels=None, q=None, within=None):
    feats = []
    for e in events:
        if bbox and not in_bbox(e, *bbox):
            continue
        if levels and (e.closure.level if e.closure else "none") not in levels:
            continue
        if within is not None and center is not None:
            d = distance_mi(e.geometry[0], e.geometry[1], center)
            if e.path:
                d = min(d, distance_mi("line", e.path, center))
            if d > within:
                continue
        if q:
            hay = " ".join(
                [e.kind or "", e.location_text, e.reason or "", " ".join(e.routes)]
                + [n.text for n in e.notes]
            ).lower()
            if q.lower() not in hay:
                continue
        feats.append(to_feature(e, center))
    return {"type": "FeatureCollection", "features": feats}


def feed_status(fetcher: Fetcher, names=None) -> dict:
    """Freshness of the cached upstream files, for the UI's 'updated X ago'."""
    out = {}
    names = names or ["v2_chp-only.kml", "v2_lcs2way.kml", "v2_lcspending.kml"]
    for n in names:
        meta_path = os.path.join(fetcher.cache_dir, n + ".meta")
        if not os.path.exists(meta_path):
            out[n] = {"fetched": False}
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        age = time.time() - meta.get("fetched_at", 0)
        out[n] = {
            "fetched": True,
            "age_seconds": round(age),
            "last_modified": meta.get("last_modified"),
            "stale": age > 600,
        }
    return out
