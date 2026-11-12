"""Turn event text into a lane-closure verdict.

Two tiers:
  * authoritative - the Caltrans Lane Closure System (LCS) layer states closures
    explicitly ("Northbound 1 Lane Closure", "Off Ramp Full Closure").
  * heuristic     - CHP incident CAD notes are free text typed by a dispatcher
    interpreting a reporting party's phone call ("BLKG # 1 LANE"), so we match
    patterns and always keep the quoted evidence visible for the human to judge.
"""

from __future__ import annotations

import re

from .model import ClosureSignal

LEVELS = ("full", "one_way", "lanes", "shoulder", "blocked", "none")
RANK = {level: i for i, level in enumerate(LEVELS)}

_DIRECTION_RE = re.compile(
    r"\b(northbound|southbound|eastbound|westbound|nb|sb|eb|wb)\b", re.I
)
_DIR_MAP = {"nb": "northbound", "sb": "southbound", "eb": "eastbound", "wb": "westbound"}

_FULL_RES = [
    r"fully\s+clos(e|d|ur)",
    r"full\s+clos",
    r"road\s+closed?",
    r"fwy\b[^.\n]{0,25}(closed|clsd|clos)",
    r"freeway[^.\n]{0,25}(closed|clsd)",
    r"(off|on)\s?ramp[^.\n]{0,20}(closed|clsd)",
    r"\ball\s+lanes?\b",
    r"\bboth\s+directions?\b",
    r"\bclosed\b(?![^.\n]{0,15}(lane|ln|shoulder|shldr))",
    r"\bclsd\b",
    r"\bdetour",
    r"divert(e|ed|ing|ion)",
]
# CHP CAD text is dispatcher shorthand: LN/LNS for lane, SLO/FAST for slow,
# BLKG/BLK for blocking. Both spellings matter - "IN SLOW LN" is a lane
# obstruction even though it never says the word "lane".
_LANES_RES = [
    r"(\d{1,2})\s*lanes?\s+(closur|clos|block|out)",
    r"block(ing|ed)?\b[^.\n]{0,40}\b(lanes?|lns?\b|hov|carpool)",
    r"blk(ing|g)?\b[^.\n]{0,40}\b(lanes?|lns?\b|hov|carpool|slo|fast|slow)",
    r"\b(lanes?|lns?)\b[^.\n]{0,25}\b(block|clos)",
    r"\bin\s+(the\s+)?(left|right|middle|fast|slow|hov|slo|carpool)(\s+#?\d{1,2})?\s+(lane|ln)\b",
    r"\b(left|right|middle|fast|slow|hov|slo|carpool)\s+(lane|ln)\b",
    r"\b#\s?\d{1,2}\s+(lane|ln)\b",
    r"\b(1|2|3|4|5)\s?st\s?ln\b",
    r"\b(both|all)\s+(lanes?|lns?)\b",
    r"\b\d{1,2}\s*(st|nd|rd|th)\s+(lane|ln)\b",
]
_SHOULDER_RES = [r"\bshoulders?\b", r"\bshldr\b", r"\bsld\b", r"\bshld\b"]
_BLOCKED_RES = [
    r"\bblocked\b",
    r"\bblocking\b",
    r"\bblk\b",
    r"\bdebris\b",
    r"\bdisabled\b",
    r"\bstuck\b",
    r"\bstruck\b",
    r"\bobstr(uction)?\b",
    r"\bhazard\b",
    r"\bdown\s+(veh|vehicle|truck|car)\b",
]


def _direction(text: str) -> str | None:
    m = _DIRECTION_RE.search(text)
    if not m:
        return None
    d = m.group(1).lower()
    return _DIR_MAP.get(d, d)


_LANE_WORD_RE = re.compile(r"\b(lanes?|lns?|ln)\b", re.I)
_OBSTRUCT_RE = re.compile(r"\b(block|blk|blkg|clos|clsd|debris|stuck|struck|down|obstr)", re.I)


def _evidence(patterns: list[str], text: str, limit: int = 3) -> list[str]:
    out: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            s = re.split(r"\s+;\s+", m.group(0).strip())[0][:70]
            if s and s not in out and not any(s in x for x in out):
                out.append(s)
            if len(out) >= limit:
                return out
    return out


def _lane_count(text: str) -> int | None:
    m = re.search(r"\b(\d{1,2})\s*lanes?\s+(?:closur|clos|block|out)", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,2})\s+lanes?\b", text, re.I)
    return int(m.group(1)) if m else None


def classify(event) -> ClosureSignal:
    title = event.title or ""
    notes = " ; ".join(n.text for n in event.notes)
    text = " ".join([title, event.kind or "", event.reason or "", event.location_text, notes])
    direction = _direction(text)

    # --- authoritative: Caltrans LCS / chain-control style layers -------------
    if event.layer in ("lane_closures", "full_closures", "chain_controls") or event.closure_id:
        ev = _evidence(
            [r"full\s+closur", r"\d+\s+lanes?\s+closur", r"one-way\s+traffic", r"off\s?ramp"],
            title or text,
        )
        level = "none"
        if re.search(r"full\s+closur", title, re.I) or event.style in (
            "full-closure",
            "fullClosurePath",
            "pending-full-closure",
        ):
            level = "full"
        elif re.search(r"one-way\s+traffic", title, re.I):
            level = "one_way"
        elif re.search(r"lanes?\s+closur", title, re.I) or event.style == "lcs":
            level = "lanes"
        lanes = event.lanes_closed if event.lanes_closed is not None else _lane_count(title)
        whole_dir = bool(
            event.lanes_closed
            and event.lanes_total
            and event.lanes_closed >= event.lanes_total
            and not re.search(r"full\s+closur", title, re.I)
        )
        evidence = [title] if title else _evidence(
            [r"full\s+closur", r"\d+\s+lanes?\s+closur", r"one-way\s+traffic", r"off\s?ramp"],
            text,
        )
        if whole_dir:
            evidence.append(
                f"{event.lanes_closed} of {event.lanes_total} lanes closed (whole direction)"
            )
        return ClosureSignal(
            level=level,
            lanes_affected=lanes,
            direction=direction,
            evidence=evidence,
            authoritative=True,
        )

    # --- heuristic: CHP incident ---------------------------------------------
    if event.style == "full-closure":
        return ClosureSignal("full", None, direction, ["CHP full-closure marker"], False)

    if _evidence(_FULL_RES, text, 1):
        return ClosureSignal(
            "full", _lane_count(text), direction, _evidence(_FULL_RES + _LANES_RES, text), False
        )
    if _evidence(_LANES_RES, text, 1):
        return ClosureSignal(
            "lanes", _lane_count(text), direction, _evidence(_LANES_RES, text), False
        )
    # lane abbreviation + an obstruction word in the same note is enough: CHP CAD
    # is terse ("DEBRIS ... ACROSS THE BOTH LNS OF TRFC").
    if any(
        _LANE_WORD_RE.search(n.text) and _OBSTRUCT_RE.search(n.text)
        for n in event.notes
    ) or (_LANE_WORD_RE.search(text) and _OBSTRUCT_RE.search(text)):
        return ClosureSignal("lanes", _lane_count(text), direction, _evidence(_LANES_RES + _BLOCKED_RES, text), False)
    if _evidence(_SHOULDER_RES, text, 1):
        return ClosureSignal(
            "shoulder", None, direction, _evidence(_SHOULDER_RES, text), False
        )
    if _evidence(_BLOCKED_RES, text, 1):
        return ClosureSignal(
            "blocked", None, direction, _evidence(_BLOCKED_RES, text), False
        )
    return ClosureSignal("none", None, direction, [], False)


def reported_by(event) -> str | None:
    """Was this pushed into CHP by a motorist (reporting party) rather than a unit?"""
    text = " ".join(n.text for n in event.notes)
    if re.search(r"\brp\b|reporting\s+party|\bcaller\b|\b911\b|\bwitness\b|reported\s+by", text, re.I):
        return "reporting party"
    if event.source == "caltrans":
        return "caltrans / contractor"
    return "chp unit"
