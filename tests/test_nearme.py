"""Offline tests: fixtures are real QuickMap placemarks, trimmed from live data."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nearme.api import events_as_geojson  # noqa: E402
from nearme.geo import distance_mi, haversine_mi  # noqa: E402
from nearme.geocode import GeocodeError, parse_point  # noqa: E402
from nearme.kml import parse_kml  # noqa: E402
from nearme.lane_closure import classify, reported_by  # noqa: E402
from nearme.query import _merge_dupes, annotate, classify_all, dedupe, nearby  # noqa: E402
from nearme.routes import extract  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SAC = (38.5816, -121.4944)


def load(name, source, layer):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return parse_kml(f.read(), source, layer)


class TestChpParsing(unittest.TestCase):
    def setUp(self):
        self.events = load("chp.kml", "chp", "chp_incidents")

    def test_all_placemarks_parse(self):
        self.assertEqual(len(self.events), 5)

    def test_fields_land_in_the_right_place(self):
        e = next(x for x in self.events if x.kind == "1183-Trfc Collision-Unkn Inj")
        self.assertEqual(e.occurred, datetime(2026, 11, 12, 6, 55))
        self.assertEqual(e.location_text, "Sr60 W / Phillips Ranch Rd")
        self.assertGreaterEqual(len(e.notes), 3)
        self.assertNotIn("AM", e.location_text)
        self.assertNotIn("Nov", e.location_text)
        self.assertTrue(e.location_text.strip())
        self.assertTrue(e.notes, "CAD log lines should be parsed as notes")
        self.assertTrue(all(n.text for n in e.notes))
        self.assertFalse(any(re.match(r"^\[\d{1,3}\]", n.text) for n in e.notes), "unit tags stripped")
        self.assertFalse(any(n.text.startswith("[Appended") for n in e.notes), "tag prefixes stripped")
        self.assertTrue(any(n.tag and n.tag.lower().startswith("appended") for n in e.notes))
        self.assertTrue(any(n.unit for n in e.notes))
        self.assertTrue(any("[CHP]" in n.text for n in e.notes), "meaningful brackets preserved")

    def test_ids_and_geometry(self):
        for e in self.events:
            self.assertEqual(e.geometry[0], "point")
            lat, lon = e.geometry[1]
            self.assertTrue(32 < lat < 43, "California latitude")
            self.assertTrue(-125 < lon < -114, "California longitude")
        self.assertTrue(any(e.event_id and e.event_id.startswith("26") for e in self.events))

    def test_updated_time_parsed(self):
        self.assertTrue(all(e.updated for e in self.events))


class TestLaneClosureSignals(unittest.TestCase):
    def setUp(self):
        self.events = load("chp.kml", "chp", "chp_incidents")

    def test_slow_lane_note_is_a_lane_closure(self):
        e = next(x for x in self.events if any("IN SLOW LN" in n.text for n in x.notes))
        c = classify(e)
        self.assertEqual(c.level, "lanes")
        self.assertTrue(c.evidence)

    def test_abbreviation_only_note_still_detected(self):
        e = next(x for x in self.events if any("LNS OF TRFC" in n.text for n in x.notes))
        self.assertEqual(classify(e).level, "lanes")

    def test_hov_lane_blocking_detected(self):
        e = next(x for x in self.events if any("HOV" in n.text for n in x.notes))
        self.assertEqual(classify(e).level, "lanes")

    def test_full_closure_style_wins(self):
        e = next(x for x in self.events if x.style == "full-closure")
        self.assertEqual(classify(e).level, "full")

    def test_reporting_party_flagged(self):
        rp = [x for x in self.events if reported_by(x) == "reporting party"]
        self.assertTrue(rp, "at least one motorist-reported incident expected")

    def test_level_ranking_is_total(self):
        from nearme.lane_closure import LEVELS

        self.assertEqual(LEVELS[0], "full")
        self.assertEqual(LEVELS[-1], "none")


class TestLcs(unittest.TestCase):
    def setUp(self):
        self.events = load("lcs.kml", "caltrans", "lane_closures")

    def test_structured_lane_counts(self):
        e = next(x for x in self.events if x.lanes_closed)
        c = classify(e)
        self.assertTrue(c.authoritative)
        self.assertIn(c.level, ("lanes", "full"))
        self.assertEqual(c.lanes_affected, e.lanes_closed)
        self.assertIsNotNone(c.direction)
        self.assertTrue(e.reason, "'Due to ...' should be captured as reason")

    def test_line_geometry_supported(self):
        lines = [x for x in self.events if x.is_line]
        self.assertTrue(lines, "fullClosurePath placemarks should carry a LineString")
        pts = lines[0].geometry[1]
        self.assertGreaterEqual(len(pts), 2)
        d_mid = distance_mi("line", pts, pts[len(pts) // 2])
        self.assertLess(d_mid, 0.01)

    def test_full_closure_title(self):
        full = [x for x in self.events if "Full Closure" in (x.title or "")]
        self.assertTrue(full)
        self.assertTrue(any(classify(e).level == "full" for e in full))


class TestGeo(unittest.TestCase):
    def test_haversine_known_distance(self):
        # Sacramento -> Davis, roughly 13 miles
        self.assertAlmostEqual(haversine_mi(SAC, (38.5557, -121.7405)), 13.4, delta=0.6)

    def test_point_off_a_line_measures_perpendicular(self):
        line = [(38.5, -121.5), (38.5, -121.4)]
        d = distance_mi("line", line, (38.55, -121.45))
        self.assertAlmostEqual(d, haversine_mi((38.5, -121.45), (38.55, -121.45)), delta=0.05)
        self.assertLess(d, haversine_mi(SAC, (38.5, -121.5)))

    def test_radius_filter_and_sort(self):
        events = annotate(load("chp.kml", "chp", "chp_incidents"), SAC)
        for e in events:
            e.closure = None
        hits = nearby(events, SAC, radius_mi=0.001)
        self.assertEqual(hits, [])
        hits = nearby(events, SAC, radius_mi=500)
        self.assertTrue(hits)
        self.assertEqual(hits, sorted(hits, key=lambda e: e.distance_mi))


class TestDedupe(unittest.TestCase):
    def test_shared_chp_records_merge(self):
        events = load("chp.kml", "chp", "chp_incidents")
        merged = dedupe(events)
        self.assertLessEqual(len(merged), len(events))
        dupe = [e for e in merged if len(e.ids) > 1]
        if dupe:
            self.assertTrue(len(dupe[0].notes) >= 2)

    def test_synthetic_same_place_merges(self):
        from nearme.model import Event, Note

        mk = lambda i: Event(
            source="chp",
            layer="chp_incidents",
            event_id=i,
            title="1125-Traffic Hazard",
            kind="1125-Traffic Hazard",
            geometry=("point", (38.58, -121.49)),
            occurred=datetime(2026, 11, 12, 7, 0),
            notes=[Note(None, f"LOG {i}")],
            ids=[i],
        )
        merged = _merge_dupes([mk("A"), mk("B")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(sorted(merged[0].ids), ["A", "B"])
        self.assertEqual(len(merged[0].notes), 2)


class TestLineDedupe(unittest.TestCase):
    def test_line_geometry_survives_dedupe(self):
        """Regression: point coordinates are tuples, dedupe used to round() them wrong."""
        events = load("lcs.kml", "caltrans", "lane_closures")
        out = dedupe(events)
        self.assertTrue(out)
        self.assertTrue(
            any(e.is_line or e.path for e in out),
            "line geometry must survive dedupe (standalone or folded into its point record)",
        )

    def test_path_polyline_folds_into_parent(self):
        events = load("lcs.kml", "caltrans", "lane_closures")
        parents = [e for e in dedupe(events) if e.path]
        for p in parents:
            self.assertGreaterEqual(len(p.path), 2)
            self.assertTrue(p.kind, "the parent record keeps the closure title")


class TestRoutes(unittest.TestCase):
    def test_prefixed_and_junction_tokens(self):
        from nearme.model import Event

        e = Event(
            source="chp",
            layer="chp_incidents",
            event_id=None,
            title="1182-Trfc Collision-No Inj",
            kind="1182-Trfc Collision-No Inj",
            geometry=("point", (38.58, -121.49)),
            location_text="I5 S / Jno 50",
        )
        numbers, labels = extract(e)
        self.assertIn("5", numbers)
        self.assertIn("50", numbers)

    def test_lcs_title_number(self):
        events = load("lcs.kml", "caltrans", "lane_closures")
        e = next(x for x in events if x.lanes_closed)
        self.assertTrue(extract(e)[0], "LCS closure should yield a route number")

    def test_route_filter_matches_across_prefixes(self):
        events = annotate(load("chp.kml", "chp", "chp_incidents"), SAC)
        for e in events:
            e.closure = None
        with_route = [e for e in events if extract(e)[0]]
        if with_route:
            num = extract(with_route[0])[0][0]
            hits = nearby(with_route, SAC, radius_mi=999, route=num)
            self.assertTrue(hits)
            self.assertTrue(all(num in e.routes for e in hits))


class TestGeoJsonLayer(unittest.TestCase):
    """The web path goes through classify_all(), not annotate() - so the level
    colouring, the level filter and route matching must all work without a point."""

    def setUp(self):
        self.events = classify_all(load("chp.kml", "chp", "chp_incidents") + load("lcs.kml", "caltrans", "lane_closures"))

    def test_events_are_classified_not_all_none(self):
        levels = {e.closure.level for e in self.events}
        self.assertNotEqual(levels, {"none"})
        self.assertIn("lanes", levels)

    def test_level_filter_actually_filters(self):
        fc = events_as_geojson(self.events, levels=["full", "lanes"])
        self.assertTrue(fc["features"])
        self.assertTrue(all(f["properties"]["level"] in ("full", "lanes") for f in fc["features"]))

    def test_direction_and_routes_reach_the_browser(self):
        fc = events_as_geojson(self.events, levels=["one_way", "full", "lanes"])
        props = [f["properties"] for f in fc["features"]]
        self.assertTrue(any(p["direction"] for p in props))
        self.assertTrue(any(p["routes"] for p in props))

    def test_bbox_and_center_distance(self):
        fc = events_as_geojson(self.events, bbox=(-121.8, 38.4, -121.3, 38.8), center=SAC)
        self.assertTrue(fc["features"])
        self.assertTrue(all(f["properties"]["distance_mi"] is not None for f in fc["features"]))
        far = events_as_geojson(self.events, bbox=(-121.8, 38.4, -121.3, 38.8), center=SAC, within=1)
        self.assertTrue(len(far["features"]) < len(fc["features"]))

    def test_line_closure_carries_its_trace(self):
        fc = events_as_geojson(self.events)
        self.assertTrue(any(f["properties"]["trace"] for f in fc["features"]))

    def test_search_hits_log_text(self):
        fc = events_as_geojson(self.events, q="lane closure")
        self.assertTrue(fc["features"])
        self.assertFalse(events_as_geojson(self.events, q="zzzznotathing")["features"])


class TestGeocodeParsing(unittest.TestCase):
    def test_coordinates(self):
        self.assertEqual(parse_point("38.5816,-121.4944"), (38.5816, -121.4944))
        self.assertEqual(parse_point("38.5816 -121.4944"), (38.5816, -121.4944))

    def test_out_of_range_rejected(self):
        with self.assertRaises(GeocodeError):
            parse_point("95.0,200.0")

    def test_not_coordinates(self):
        self.assertIsNone(parse_point("95814"))
        self.assertIsNone(parse_point("Sacramento, CA"))


class TestStaticParity(unittest.TestCase):
    """web/static.js (the GitHub Pages build, which has no server) must filter
    exactly like api.py (which the local server uses). Two implementations of one
    rule set - this test is what keeps them from drifting apart."""

    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node not on PATH")
        events = load("chp.kml", "chp", "chp_incidents") + load("lcs.kml", "caltrans", "lane_closures")
        self.events = classify_all(dedupe(events))

    def queries(self):
        qs = [
            {"key": "statewide"},
            {"key": "bbox_sac", "bbox": (-121.7, 38.4, -121.3, 38.8)},
            {"key": "levels", "levels": ["full", "lanes"]},
            {"key": "center", "center": SAC},
            {"key": "within_5", "center": SAC, "radius": 5.0},
            {"key": "bbox+center+within", "bbox": (-121.7, 38.4, -121.3, 38.8), "center": SAC, "radius": 20.0},
        ]
        traced = next((e for e in self.events if e.path), None)
        if traced:
            # A box around one vertex of a pavement trace: only the 0.04 deg pad in
            # in_bbox() can catch this closure, so it exercises the pad rule.
            lat, lon = traced.path[len(traced.path) // 2]
            qs.append({"key": "trace_only", "bbox": (lon - 0.002, lat - 0.002, lon + 0.002, lat + 0.002)})
        return qs

    def run_js(self, snapshot, queries):
        payload = json.dumps({"snapshot": snapshot, "queries": queries})
        proc = subprocess.run(
            [self.node, os.path.join(os.path.dirname(__file__), "static_parity.js")],
            input=payload.encode("utf-8"),
            capture_output=True,
        )
        if proc.returncode:
            self.fail(f"node harness failed: {proc.stderr.decode('utf-8', 'replace')}")
        return json.loads(proc.stdout.decode("utf-8"))

    def test_js_matches_python(self):
        snapshot = events_as_geojson(self.events)  # what build_site.py publishes
        queries = self.queries()
        got = {r["key"]: r["features"] for r in self.run_js(snapshot, queries)}
        self.assertEqual(len(got), len(queries))
        for q in queries:
            want = events_as_geojson(
                self.events,
                bbox=q.get("bbox"),
                levels=q.get("levels"),
                center=q.get("center"),
                within=q.get("radius"),
            )
            expected = {
                f["properties"]["uid"]:
                    f["properties"].get("distance_mi") for f in want["features"]
            }
            actual = {f["uid"]: f["distance_mi"] for f in got[q["key"]]}
            if q.get("bbox") and q["key"] in ("bbox_sac", "trace_only"):
                # guard against a filter that silently drops everything
                self.assertTrue(expected, f"{q['key']}: python found nothing to compare")
                self.assertTrue(actual, f"{q['key']}: js found nothing")
            self.assertEqual(
                set(actual), set(expected), f"{q['key']}: feature sets differ ({len(expected)} python)"
            )
            for uid, d in expected.items():
                if d is None:
                    self.assertIsNone(actual[uid], f"{q['key']}/{uid}: distance leaked in")
                else:
                    self.assertAlmostEqual(
                        actual[uid], d, delta=0.02, msg=f"{q['key']}/{uid}: {actual[uid]} != {d}"
                    )

    def test_snapshot_is_loadable_by_the_browser_filter(self):
        snapshot = events_as_geojson(self.events)
        got = self.run_js(snapshot, [{"key": "statewide"}])[0]["features"]
        self.assertGreater(len(got), 0)
        self.assertEqual(len(got), len(snapshot["features"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
