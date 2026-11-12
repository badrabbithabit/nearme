"""Pull state-route numbers out of event text so you can ask "is my commute hit?".

Text is messy: "I5 S / Jno 50", "SR94 W SR125 EO", "Route 162 One-way Traffic
Operation", "Northbound 5 Off Ramp Full Closure". We normalize to bare route
numbers (5, 50, 101) because a driver asking about "50" wants I-50 and SR-50
both surfaced, and keep the original tokens for display.
"""

from __future__ import annotations

import re

_PREFIXED_RE = re.compile(
    r"\b(i|ih|us|sr|ca|state route|route|frwy|fwy|hwy|jno|jh|junction)\s*-?\s*(\d{1,3})\b",
    re.I,
)
# LCS titles put the number between the direction and the closure phrase.
_LCS_RE = re.compile(
    r"\b(?:northbound|southbound|eastbound|westbound)?\s*(\d{1,3})\s+"
    r"(on ramp|off ramp|ramp|lane closure|full closure|lane|one-way)",
    re.I,
)
_LABEL_RE = re.compile(r"\b(i|us|sr)?\s*-?\s*\d{1,3}\b", re.I)


def extract(event) -> tuple[list[str], list[str]]:
    """Return (route numbers, human labels) for an event."""
    blobs = [event.kind or "", event.title or "", event.location_text, event.reason or ""]
    blobs += [n.text for n in event.notes]
    numbers: list[str] = []
    labels: list[str] = []
    for blob in blobs:
        for m in _PREFIXED_RE.finditer(blob):
            num = m.group(2)
            if num not in numbers:
                numbers.append(num)
            label = f"{m.group(1)[0].upper()}{num}" if m.group(1)[0].lower() != "j" else m.group(0)
            if label not in labels:
                labels.append(label)
        for m in _LCS_RE.finditer(blob):
            num = m.group(1)
            if num not in numbers:
                numbers.append(num)
                if num not in labels:
                    labels.append(num)
    return numbers, labels
