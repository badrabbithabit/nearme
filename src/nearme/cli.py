"""Command line interface for the near-me traffic checker."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from .fetch import Fetcher
from .geocode import Geocoder, GeocodeError
from .geo import polyline_len_mi
from .lane_closure import RANK
from .model import Event
from .query import DEFAULT_LAYERS, annotate, gather, nearby

LEVEL_TAG = {
    "full": "FULL CLOSURE",
    "one_way": "ONE-WAY TRAFFIC",
    "lanes": "LANE CLOSURE",
    "shoulder": "SHOULDER",
    "blocked": "BLOCKAGE",
    "none": "no closure reported",
}


def load_config(path: str | None) -> dict:
    candidates = [path] if path else ["config.json", os.path.join(os.path.dirname(__file__), "..", "..", "config.json")]
    for c in candidates:
        if c and os.path.exists(c):
            with open(c, encoding="utf-8") as f:
                return json.load(f)
    return {}


def fmt_when(when: datetime | None) -> str:
    return when.strftime("%m/%d %H:%M") if when else "--"


def describe(e: Event, show_notes: int) -> str:
    lines: list[str] = []
    src = "CHP" if e.source == "chp" else "Caltrans"
    lines.append(
        f"{e.distance_mi:>5.1f} mi  {src:<8} {fmt_when(e.occurred or e.updated):<12} "
        f"{e.kind or e.title or '(untitled)'}"
    )
    where = e.location_text or "(no location text)"
    lines.append(f"         where : {where}")
    tag = LEVEL_TAG[e.closure.level] if e.closure else "no closure reported"
    extra = []
    if e.closure and e.closure.lanes_affected:
        extra.append(f"{e.closure.lanes_affected} lane(s)")
    if e.closure and e.closure.direction:
        extra.append(e.closure.direction)
    if e.closure and e.closure.authoritative:
        extra.append("LCS record")
    lines.append(f"         closure : {tag}{' / ' + ', '.join(extra) if extra else ''}")
    if e.closure and e.closure.evidence:
        ev = ' // '.join(f"'{x[:70]}'" for x in e.closure.evidence[:3])
        lines.append(f"         quote   : {ev}")
    if e.reported_by:
        lines.append(f"         source  : {e.reported_by}")
    if e.reason:
        lines.append(f"         reason  : {e.reason}")
    if e.expected_end:
        lines.append(f"         until   : {e.expected_end.strftime('%m/%d %H:%M')}")
    if e.path:
        lines.append(f"         trace   : ~{polyline_len_mi(e.path):.1f} mi of roadway affected")
    if e.routes:
        lines.append(f"         routes  : {', '.join(e.routes)}")
    if e.lanes_closed:
        lines.append(f"         lanes   : {e.lanes_closed} of {e.lanes_total} closed")
    if e.closure_id or e.event_id:
        ids = e.ids or [e.closure_id or e.event_id]
        lines.append(f"         id      : {', '.join(i for i in ids if i)}")
    for n in (e.notes or [])[-show_notes:] if show_notes else []:
        who = f"[{n.unit}] " if n.unit else ""
        lines.append(f"         log     : {fmt_when(n.when)} {who}{n.text[:160]}")
    return "\n".join(lines)


def cmd_near(args: argparse.Namespace, cfg: dict) -> int:
    fetcher = Fetcher(cache_dir=args.cache_dir, min_refresh_seconds=args.min_refresh)
    geocoder = Geocoder(cache_dir=args.cache_dir, places=cfg.get("places", {}))
    at = args.at or cfg.get("at")
    if not at:
        print("No location given. Use --at, or set \"at\" in config.json.", file=sys.stderr)
        return 2
    try:
        point, label = geocoder.resolve(at)
    except GeocodeError as e:
        print(str(e), file=sys.stderr)
        return 2

    layers = tuple(args.layers.split(",")) if args.layers else tuple(cfg.get("layers", DEFAULT_LAYERS))
    events, statuses = gather(fetcher, layers, force=args.force)
    annotate(events, point)
    min_level = None
    if args.lanes_only:
        min_level = "lanes"
    hits = nearby(
        events,
        point,
        radius_mi=args.radius,
        min_level=min_level,
        kind_pattern=args.grep,
        max_age_minutes=args.max_age,
        limit=args.max,
        route=args.route,
    )

    print(f"\nnear {label}  (radius {args.radius} mi, layers: {', '.join(layers)})")
    for s in statuses:
        print(f"  feed: {s}")
    print(f"  {len(hits)} of {len(events)} events within {args.radius} mi\n")

    if args.json:
        print(json.dumps([e.to_dict() for e in hits], indent=1, ensure_ascii=False))
        return 0
    if not hits:
        print("  nothing reported. (Try --radius 30, or --force to bypass the cache TTL.)")
        return 0
    for e in hits:
        print(describe(e, args.notes))
        print()
    print(
        "  Data courtesy of CHP / Caltrans via QuickMap. Free-text CHP notes are\n"
        "  dispatcher transcriptions of reports from motorists - treat quotes as leads,\n"
        "  not confirmations. Drive accordingly."
    )
    return 0


def cmd_layers(args: argparse.Namespace, cfg: dict) -> int:
    fetcher = Fetcher(cache_dir=args.cache_dir, min_refresh_seconds=args.min_refresh)
    for lid, d in sorted(fetcher.layers(force=args.force).items()):
        print(f"{lid:<22} {d.geometry:<12} {d.source:<9} {' '.join(d.files)}")
    return 0


def cmd_watch(args: argparse.Namespace, cfg: dict) -> int:
    args.at = args.at or cfg.get("at")
    seen: dict[str, Event] = {}
    while True:
        try:
            code = cmd_near(args, cfg)
        except Exception as e:  # keep the loop alive
            print(f"watch: {e}", file=sys.stderr)
            code = 1
        print(f"\n[{datetime.now():%H:%M:%S}] next refresh in {args.interval}s (ctrl-c to stop)")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nearme",
        description="What is closed near me? CHP incidents + Caltrans lane closures.",
    )
    p.add_argument("--config", help="path to config.json")
    p.add_argument("--cache-dir", default=".cache")
    p.add_argument("--min-refresh", type=int, default=180, help="seconds between network fetches")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--at", help='"38.58,-121.49" | ZIP | place name | name from config places')
        sp.add_argument("--radius", type=float, default=10.0, help="miles")
        sp.add_argument("--layers", help=f"comma separated (default {','.join(DEFAULT_LAYERS)})")
        sp.add_argument("--lanes-only", action="store_true", help="only lane/full closures")
        sp.add_argument("--route", help="only events touching this route number, e.g. 50")
        sp.add_argument("--grep", help="filter on kind/text regex")
        sp.add_argument("--max-age", type=int, help="drop events older than N minutes")
        sp.add_argument("--max", type=int, default=25, help="max rows")
        sp.add_argument("--notes", type=int, default=2, help="CAD log lines to show")
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--force", action="store_true", help="ignore cache TTL")

    sp = sub.add_parser("near", help="events near a point")
    common(sp)
    sp.set_defaults(func=cmd_near)

    sp = sub.add_parser("watch", help="refresh on an interval")
    common(sp)
    sp.add_argument("--interval", type=int, default=180)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("layers", help="list available QuickMap layers")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_layers)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd in ("near", "watch") and getattr(args, "radius", None) == 10.0:
        args.radius = float(cfg.get("radius_mi", args.radius))
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
