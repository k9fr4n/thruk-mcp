"""Shared monitoring constants: state maps used across server.py and filters.py.

Centralising these here avoids silent divergence when a new Naemon state is
added: a single edit to this file propagates everywhere.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Host state maps
# ---------------------------------------------------------------------------

#: int -> human-readable label  (e.g. 0 -> "UP")
HOST_STATE_STR: dict[int, str] = {0: "UP", 1: "DOWN", 2: "UNREACHABLE"}

#: string / numeric-string -> int  (e.g. "down" -> 1, "1" -> 1)
HOST_STATE_INT: dict[str, int] = {
    "up": 0,
    "down": 1,
    "unreachable": 2,
    "0": 0,
    "1": 1,
    "2": 2,
}

# ---------------------------------------------------------------------------
# Service state maps
# ---------------------------------------------------------------------------

#: int -> human-readable label  (e.g. 2 -> "CRITICAL")
SVC_STATE_STR: dict[int, str] = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN"}

#: string / numeric-string -> int  (e.g. "critical" -> 2, "2" -> 2)
SVC_STATE_INT: dict[str, int] = {
    "ok": 0,
    "warning": 1,
    "critical": 2,
    "unknown": 3,
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
}
