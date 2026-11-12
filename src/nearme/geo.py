"""Distance math: haversine for points, point-to-polyline for lane closures."""

from __future__ import annotations

import math

EARTH_MI = 3958.7613


def haversine_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_MI * math.asin(math.sqrt(h))


def _to_xy(p: tuple[float, float], origin: tuple[float, float]) -> tuple[float, float]:
    """Local tangent-plane projection in miles (fine for segments < ~50 mi)."""
    lat0 = math.radians(origin[0])
    x = math.radians(p[1] - origin[1]) * EARTH_MI * math.cos(lat0)
    y = math.radians(p[0] - origin[0]) * EARTH_MI
    return x, y


def dist_to_segment_mi(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    px, py = _to_xy(p, a)
    bx, by = _to_xy(b, a)
    if bx == by == 0:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * bx + py * by) / (bx * bx + by * by)))
    return math.hypot(px - t * bx, py - t * by)


def distance_mi(geom_type: str, geom, p: tuple[float, float]) -> float:
    """Distance from point p to a 'point' or 'line' geometry, in miles."""
    if geom_type == "point":
        return haversine_mi(p, geom)
    best = float("inf")
    for a, b in zip(geom, geom[1:]):
        best = min(best, dist_to_segment_mi(p, a, b))
    if best == float("inf"):
        best = min(haversine_mi(p, v) for v in geom)
    return best


def polyline_len_mi(pts: list[tuple[float, float]]) -> float:
    return sum(haversine_mi(a, b) for a, b in zip(pts, pts[1:]))
