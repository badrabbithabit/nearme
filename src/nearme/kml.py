"""Parse Caltrans QuickMap KML layers into Event records.

QuickMap has no JSON API; every layer is KML whose Placemark description holds a
pre-rendered Google Maps infowindow (HTML). We parse that HTML into fields.
Format observed 2026-11 (v2 layers):

  CHP Incident <id>            / <h2 class="iw-title">1182-Trfc Collision-No Inj</h2>
                               / <p class="iw-text">Nov 12 2026  7:17AM <br> Erle Rd / Duggin St</p>
                               / <p class="iw-text">Nov 12 2026 7:19AM [5] RP ...<br />...</p>
  Lane Closure                 / <h2 class="iw-title">Northbound 1 Lane Closure</h2>
                               / <p class="iw-text">From ... to ...</p>
                               / <p class="iw-text"> Due to Bridge Work</p>
                               / <p class="iw-text">Expected to end at 7:01am Jan 29, 2027</p>
                               / <div>Closure ID: C162HA, Log Number: 1</div>
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from xml.etree import ElementTree as ET

from .model import ClosureSignal, Event, Note

KML = "{http://www.opengis.net/kml/2.2}"

_TIME_FORMATS = (
    "%b %d %Y %I:%M%p",
    "%b %d %Y %I:%M %p",
    "%B %d, %Y %I:%M%p",
    "%m/%d/%Y %I:%M%p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
)
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
# "Nov 12 2026  7:17AM" optionally followed by the rest of the line
_TS_RE = re.compile(
    r"^(?P<ts>(?:%s)\s+\d{1,2}\s+\d{4}\s+\d{1,2}:\d{2}\s*[APap][Mm])\s*(?P<rest>.*)$" % _MONTHS,
    re.I,
)
_END_RE = re.compile(
    rf"(?P<md>\d{{1,2}}:\d{{2}}\s*[ap]m)\s+(?P<m>{_MONTHS})[a-z]*\.?\s+(?P<d>\d{{1,2}}),\s*(?P<y>\d{{4}})",
    re.I,
)


def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"(\d)\s+([APap][Mm])", r"\1 \2", t).replace(" AM", "AM").replace(" PM", "PM")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    # "7:01am Jan 29, 2027" style
    m = _END_RE.search(t)
    if m:
        cand = f"{m.group('m')} {m.group('d')} {m.group('y')} {m.group('md').upper()}"
        cand = re.sub(r"([AP]M)\s", r"\1", cand)
        try:
            return datetime.strptime(cand, "%b %d %Y %I:%M%p")
        except ValueError:
            return None
    return None


def _last_updated(desc: str) -> datetime | None:
    m = re.search(
        r"Last updated:\s*<strong>([^<]*)</strong>\s*([0-9]{1,2}:[0-9]{2}\s*[ap]m)", desc, re.I
    )
    return _parse_dt(f"{m.group(1)} {m.group(2)}") if m else None


def _strip(html_text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html_text)
    text = re.sub(r"</(p|div|h2|span)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _note_from(when_text: str | None, body: str) -> Note:
    """CAD log lines are prefix-token soup: the unit number and system tags can
    appear in either order ("[10] [Appended, 07:04:13] [4] RP ADV ..."), and
    meaningful brackets like "[CHP]-1039" must be left alone."""
    when = _parse_dt(when_text) if when_text else None
    body = body.strip()
    tag = unit = None
    for _ in range(4):
        m = re.match(r"\[([^\]]{1,40})\]\s*(.*)$", body, re.S)
        if not m:
            break
        tok, rest = m.group(1).strip(), m.group(2)
        if re.fullmatch(r"\d{1,3}", tok):
            # CAD sometimes doubles the unit tag ("[2] [2] 1125 IN SLOW LN")
            if unit is None:
                unit = tok
            body = rest
        elif tag is None and re.match(
            r"(shared|appended|removed|dupe|notification|alert|update|info|clr)", tok, re.I
        ):
            tag, body = tok, rest
        else:
            break
    body = re.sub(r"\[Shared\]\s*$", "", body, flags=re.I).strip()
    return Note(when=when, text=re.sub(r"\s+", " ", body).strip(), unit=unit, tag=tag)


def _header_id(desc: str) -> str | None:
    m = re.search(r"(?:CHP Incident|Closure|Incident|Chain Control|Alert)\s*:?\s*([A-Z0-9-]{4,})", desc)
    return m.group(1) if m else None


def _coords(el) -> tuple[str, object] | None:
    # Closure paths come back as <MultiGeometry> wrapping a LineString (+ Point).
    multi = el.find(f"{KML}MultiGeometry")
    if multi is not None:
        el = multi
    ls = el.find(f"{KML}LineString")
    if ls is not None:
        pts = []
        for c in ls.findtext(f"{KML}coordinates", "").split():
            lon, lat = (float(v) for v in c.split(",")[:2])
            pts.append((lat, lon))
        if len(pts) >= 2:
            return ("line", pts)
    pt = el.find(f"{KML}Point")
    if pt is not None:
        raw = pt.findtext(f"{KML}coordinates", "").split()
        if raw:
            lon, lat = (float(v) for v in raw[0].split(",")[:2])
            return ("point", (lat, lon))
    return None


def parse_kml(kml_text: str, source: str, layer: str) -> list[Event]:
    root = ET.fromstring(kml_text)
    events: list[Event] = []
    for pm in root.iter(f"{KML}Placemark"):
        desc = pm.findtext(f"{KML}description", "") or ""
        geom = _coords(pm)
        if geom is None:
            continue
        title = (pm.findtext(f"{KML}name") or "").strip()
        m = re.search(r'iw-title">\s*([^<]*)<', desc)
        if m and m.group(1).strip():
            title = m.group(1).strip()
        texts = [t.strip() for t in re.findall(r'class="iw-text"[^>]*>(.*?)</p>', desc, re.S)]
        texts = [_strip(t) for t in texts if _strip(t)]

        occurred = updated = expected_end = None
        location_text, reason, notes = "", None, []
        lanes_closed = lanes_total = None
        texts = [t for t in texts if "Last updated" not in t]

        for block in texts:
            for line in [ln.strip() for ln in block.split("\n") if ln.strip()]:
                mt = _TS_RE.match(line)
                if mt:
                    ts = _parse_dt(mt.group("ts"))
                    rest = mt.group("rest").strip()
                    if not rest:
                        occurred = occurred or ts
                    else:
                        notes.append(_note_from(mt.group("ts"), rest))
                    continue
                ml = re.match(r"(\d{1,2})\s+of\s+(\d{1,2})\s+.*lanes?\s+closed", line, re.I)
                if ml:
                    lanes_closed, lanes_total = int(ml.group(1)), int(ml.group(2))
                    mdue = re.search(r"due to\s+(.+)", line, re.I)
                    if mdue:
                        reason = mdue.group(1).strip()
                    continue
                me = re.search(r"Expected to end at\s+(.+)", line, re.I)
                if me:
                    expected_end = _parse_dt(me.group(1))
                    continue
                if re.match(r"^(From|Between|At|Location)\b", line, re.I):
                    location_text = (location_text + " " + line).strip()
                    continue
                if line.lower().startswith("due to"):
                    reason = line[6:].strip()
                    continue
                if not location_text:
                    location_text = line
                elif not reason:
                    reason = line
                else:
                    notes.append(_note_from(None, line))

        closure_id = None
        mc = re.search(r"Closure ID:\s*([A-Z0-9-]+)", desc, re.I)
        if mc:
            closure_id = mc.group(1)
        if closure_id is None:
            # LCS path placemarks carry no infowindow, just "C162HA Log 1 path"
            mp = re.match(r"([A-Z]\d{1,4}[A-Z]{0,3})\s+Log\s+\d+\s+path", title or "", re.I)
            if mp:
                closure_id = mp.group(1).upper()

        event_id = _header_id(desc) or closure_id
        events.append(
            Event(
                source=source,
                layer=layer,
                event_id=event_id,
                title=title,
                kind=title,
                geometry=geom,
                occurred=occurred,
                updated=updated or _last_updated(desc),
                expected_end=expected_end,
                location_text=location_text,
                reason=reason,
                notes=notes,
                ids=[event_id] if event_id else [],
                closure_id=closure_id,
                lanes_closed=lanes_closed,
                lanes_total=lanes_total,
                style=(pm.findtext(f"{KML}styleUrl") or "").lstrip("#") or None,
                raw_text=_strip(desc)[:800],
            )
        )
    return events
