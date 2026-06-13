"""Parse a pasted match URL / id into a numeric FotMob matchId.

Accepted forms include:
    https://www.fotmob.com/matches/mallorca-vs-sevilla/xyz#4506769
    https://www.fotmob.com/match/4506769/matchfacts
    ...?matchId=4506769
    a bare numeric id
"""
from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"matchId=(\d+)"),
    re.compile(r"#(?:id[:=])?(\d{4,})"),
    re.compile(r"/match(?:es)?/[^#?]*?(\d{4,})"),
]
_BARE_RE = re.compile(r"^\s*(\d{4,})\s*$")
_TRAILING_RE = re.compile(r"(\d{5,})(?!.*\d)")


def parse_event_id(raw: str) -> int:
    """Return the numeric matchId from a URL or id string.

    Raises ``ValueError`` if no id can be found.
    """
    if not raw:
        raise ValueError("Empty input")
    raw = raw.strip()

    m = _BARE_RE.match(raw)
    if m:
        return int(m.group(1))

    for pat in _PATTERNS:
        m = pat.search(raw)
        if m:
            return int(m.group(1))

    m = _TRAILING_RE.search(raw)
    if m:
        return int(m.group(1))

    raise ValueError("Could not find a match id in the input.")
