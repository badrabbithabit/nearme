"""Data model for traffic events parsed from Caltrans QuickMap KML layers."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

# Geometry is either ("point", (lat, lon)) or ("line", [(lat, lon), ...]).
Geometry = tuple[str, Any]


@dataclass
class Note:
    """One line of a CHP incident's CAD log (often the reporting party's words)."""

    when: datetime | None
    text: str
    unit: str | None = None  # CAD unit/position number in brackets, e.g. "[5]"
    tag: str | None = None  # "[Shared]", "[Appended, 06:45:32]"


@dataclass
class ClosureSignal:
    """Heuristic verdict about whether/how much of the roadway is affected."""

    level: str  # "full" | "lanes" | "shoulder" | "blocked" | "none"
    lanes_affected: int | None
    direction: str | None  # "northbound" | "southbound" | ...
    evidence: list[str] = field(default_factory=list)
    authoritative: bool = False  # True when it came from the Caltrans LCS layer


@dataclass
class Event:
    source: str  # "chp" | "caltrans"
    layer: str  # layer id from QuickMap config/layers.json
    event_id: str | None
    title: str
    kind: str  # e.g. "1182-Trfc Collision-No Inj", "Northbound 1 Lane Closure"
    geometry: Geometry
    occurred: datetime | None = None
    updated: datetime | None = None
    expected_end: datetime | None = None
    location_text: str = ""
    reason: str | None = None
    notes: list[Note] = field(default_factory=list)
    closure_id: str | None = None
    style: str | None = None
    raw_text: str = ""
    lanes_closed: int | None = None  # from LCS "1 of 4 general purpose lanes closed"
    lanes_total: int | None = None
    ids: list[str] = field(default_factory=list)  # merged duplicate record ids
    routes: list[str] = field(default_factory=list)  # state route numbers touched
    path: list[tuple[float, float]] | None = None  # closure pavement trace, when known

    # filled in by the query layer
    distance_mi: float | None = None
    reported_by: str | None = None  # "reporting party" when a motorist called it in
    closure: ClosureSignal | None = None

    @property
    def is_line(self) -> bool:
        return self.geometry[0] == "line"

    def label(self) -> str:
        return self.title or self.kind or self.source

    def to_dict(self) -> dict:
        d = asdict(self)
        d["geometry"] = {
            "type": "point" if self.geometry[0] == "point" else "line",
            "coordinates": self.geometry[1],
        }
        for k in ("occurred", "updated", "expected_end"):
            if d[k] is not None:
                d[k] = d[k].isoformat(timespec="minutes")
        d["notes"] = [
            {**n, "when": n["when"].isoformat(timespec="minutes") if n["when"] else None}
            for n in d["notes"]
        ]
        return d
