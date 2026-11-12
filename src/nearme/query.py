"""Assemble events from several layers and answer "what is near this point?"."""

from __future__ import annotations

import re
from datetime import datetime

from .fetch import Fetcher
from .geo import distance_mi, haversine_mi
from .lane_closure import RANK, classify, reported_by
from .routes import extract as extract_routes
from .model import Event

DEFAULT_LAYERS = ("chp_incidents", "lane_closures")


def _merge_dupes(events: list[Event]) -> list[Event]:
    """One crash is often published twice: a local CHP record and an FSP "[Shared]"
    record with the other com center's log. Same place, same kind, minutes apart -
    collapse them and union the log lines so the reader sees one incident."""
    buckets: dict[tuple, list[Event]] = {}
    out: list[Event] = []
    for e in events:
        if e.source != "chp" or e.geometry[0] != "point":
            out.append(e)
            continue
        lat, lon = e.geometry[1]
        key = ((e.kind or "").lower(), round(lat, 2), round(lon, 2))
        merged = False
        for prev in buckets.setdefault(key, []):
            plat, plon = prev.geometry[1]
            close = haversine_mi((lat, lon), (plat, plon)) < 0.25
            when, pwhen = e.occurred or e.updated, prev.occurred or prev.updated
            intime = bool(when and pwhen and abs((when - pwhen).total_seconds()) < 20 * 60)
            if close and intime:
                prev.notes.extend(n for n in e.notes if n.text not in {m.text for m in prev.notes})
                prev.ids.extend(i for i in e.ids if i not in prev.ids)
                prev.location_text = prev.location_text or e.location_text
                merged = True
                break
        if not merged:
            buckets[key].append(e)
            out.append(e)
    return out


def _link_paths(events: list[Event]) -> list[Event]:
    """LCS publishes every closure twice: a point carrying the infowindow, and a
    bare "C162HA Log 1 path" polyline with no text of its own. Fold the polyline
    into its point record - otherwise the list fills up with untitled rows."""
    by_id = {e.closure_id: e for e in events if e.closure_id and not e.is_line}
    out: list[Event] = []
    for e in events:
        if e.is_line and e.closure_id and not e.raw_text and e.closure_id in by_id:
            by_id[e.closure_id].path = e.geometry[1]
            continue
        out.append(e)
    return out


def _route_from_closure_id(closure_id: str) -> str | None:
    m = re.match(r"[A-Z](\d{1,4})", closure_id or "")
    return m.group(1) if m else None


def dedupe(events: list[Event]) -> list[Event]:
    """The full_closures layer re-uses the same KML files, so events repeat."""
    seen: dict[tuple, Event] = {}
    for e in events:
        kind_rank = 0 if e.layer == "lane_closures" else 1
        coords = e.geometry[1]
        flat = coords if e.is_line else [coords]
        key = (
            (e.event_id or "") + (e.closure_id or ""),
            "line" if e.is_line else "point",  # keep point + its path polyline
            (
                e.source,
                (e.kind or "").lower(),
                tuple(tuple(round(v, 4) for v in pt) for pt in flat),
            )
            if not (e.event_id or e.closure_id)
            else None,
        )
        prev = seen.get(key)
        if prev is None or kind_rank < (0 if prev.layer == "lane_closures" else 1):
            seen[key] = e
    merged = _merge_dupes(list(seen.values()))
    return _link_paths(merged)


def gather(
    fetcher: Fetcher, layers: tuple[str, ...] | list[str] = DEFAULT_LAYERS, force: bool = False
) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    statuses: list[str] = []
    for layer in layers:
        res = fetcher.fetch_layer(layer, force=force)
        flag = " (STALE)" if res.stale else ""
        statuses.append(f"{layer}: {len(res.events)} events{flag} [{res.note}]")
        events.extend(res.events)
    return dedupe(events), statuses


def classify_all(events: list[Event]) -> list[Event]:
    """Attach the point-independent verdicts: closure level, who reported it,
    which routes it touches. The web layer needs these before any location is
    known, so this is split out from annotate()."""
    for e in events:
        e.closure = classify(e)
        e.reported_by = reported_by(e)
        e.routes = extract_routes(e)[0]
        if not e.routes:
            from_closure = _route_from_closure_id(e.closure_id or "")
            if from_closure:
                e.routes = [from_closure]
    return events


def annotate(events: list[Event], point: tuple[float, float]) -> list[Event]:
    """classify_all() plus distance from `point`. Idempotent, so calling it twice
    (or after classify_all) is harmless."""
    classify_all(events)
    for e in events:
        d = distance_mi(e.geometry[0], e.geometry[1], point)
        if e.path:
            d = min(d, distance_mi("line", e.path, point))
        e.distance_mi = round(d, 2)
        e.closure = classify(e)
    return events


def nearby(
    events: list[Event],
    point: tuple[float, float],
    radius_mi: float = 10.0,
    min_level: str | None = None,
    kind_pattern: str | None = None,
    max_age_minutes: int | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    route: str | None = None,
) -> list[Event]:
    now = now or datetime.now()
    rx = re.compile(kind_pattern, re.I) if kind_pattern else None
    out: list[Event] = []
    for e in events:
        if e.distance_mi is None:
            continue
        if e.distance_mi > radius_mi:
            continue
        if route and route.lstrip("0") not in [r.lstrip("0") for r in e.routes]:
            continue
        if min_level and e.closure and RANK.get(e.closure.level, 9) > RANK[min_level]:
            continue
        if rx and not rx.search(" ".join([e.kind or "", e.location_text, e.raw_text])):
            continue
        if max_age_minutes:
            when = e.occurred or e.updated
            if when and (now - when).total_seconds() > max_age_minutes * 60:
                continue
        out.append(e)
    out.sort(key=lambda e: (e.distance_mi, RANK.get(e.closure.level if e.closure else "none", 9)))
    return out[:limit] if limit else out
