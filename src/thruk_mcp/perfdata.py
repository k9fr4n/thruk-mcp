"""Nagios/Naemon ``perf_data`` parser and threshold-range semantics (issue #284).

Every host and service in Thruk carries a ``perf_data`` column in the standard
Nagios plugin format::

    'label'=value[UOM];warn;crit;min;max  'label2'=...

This module turns that raw string into a list of structured metric dicts and
computes a *range-correct* ``breached`` flag following the official Nagios
plugin guidelines -- NOT a simplified ``value > warn`` comparison:

* https://nagios-plugins.org/doc/guidelines.html#AEN200       (perfdata format)
* https://nagios-plugins.org/doc/guidelines.html#THRESHOLDFORMAT (range spec)

Design notes / acceptance criteria handled here
------------------------------------------------
1. Quoted labels may contain spaces, ``:`` and ``*`` -- the tokenizer never
   splits inside single quotes.
2. ``warn`` / ``crit`` are kept verbatim as raw strings (they may be Nagios
   ranges such as ``-2000:2000`` or ``@10:20``) while ``breached`` is computed
   with real range semantics.
3. An empty ``perf_data`` string yields ``[]`` -- never an exception.
4. Threshold direction is not assumed to be "higher = worse": ``breached`` and
   the proximity helper are both derived from the parsed range, so an inverted
   "days remaining" metric (``warn`` < ``crit``) is handled by the spec rather
   than a monotonic-up heuristic.
5. Missing trailing fields (value-only, empty ``warn``/``crit``) degrade to
   ``None`` without crashing.
6. Unit-of-measure variety (``B``, ``%``, ``ms``, ``s`` or none) is preserved.

The module is intentionally dependency-free (stdlib only) and pure/synchronous
-- all I/O lives in the tool layer (:mod:`thruk_mcp.tools.perfdata`).
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = [
    "breaches_range",
    "parse_metric",
    "parse_perfdata",
    "parse_range",
    "proximity_percent",
]

# A single perfdata datum is ``LABEL=REST`` where LABEL is either a
# single-quoted string (may contain spaces, ``:``, ``*`` ...) or an unquoted run
# with no whitespace / ``=``. REST is the value plus ``;``-separated fields and
# never contains whitespace, so ``\\S+`` is safe. ``finditer`` skips any
# garbage / stray separators between data points without raising.
_METRIC_RE = re.compile(r"(?:'(?P<qlabel>[^']*)'|(?P<label>[^'=\s]+))=(?P<rest>\S+)")

# Leading signed number (int, float, or scientific notation) optionally
# followed by a unit-of-measure suffix. Anchored so a non-numeric value such as
# Nagios' "U" (undetermined) fails to match and is reported as ``value=None``.
_VALUE_RE = re.compile(r"^(?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(?P<uom>.*)$")


def _num(token: str) -> float | int | None:
    """Parse a numeric perfdata field, returning ``int`` when integral.

    Returns ``None`` for empty / non-numeric tokens so callers can treat a
    missing ``min``/``max`` as absent rather than crashing.
    """
    token = token.strip()
    if not token:
        return None
    if re.fullmatch(r"[-+]?\d+", token):
        return int(token)
    try:
        return float(token)
    except ValueError:
        return None


def _range_bound(token: str, *, empty: float, tilde: float) -> float | None:
    """Resolve one side of a Nagios threshold range to a float.

    ``''`` (empty) -> ``empty`` default; ``'~'`` (infinity marker) -> ``tilde``;
    otherwise the parsed number. Returns ``None`` when the token is present but
    not numeric (the whole range is then treated as unparseable).
    """
    token = token.strip()
    if not token:
        return empty
    if token == "~":
        return tilde
    val = _num(token)
    return float(val) if val is not None else None


def parse_range(spec: str | None) -> tuple[float, float, bool] | None:
    """Parse a Nagios threshold range into ``(low, high, alert_inside)``.

    Follows the official threshold-range spec::

        10        ->  0:10     alert if value < 0   or value > 10
        10:       ->  10:inf   alert if value < 10
        ~:10      -> -inf:10    alert if value > 10
        10:20     ->  10:20     alert if value < 10  or value > 20
        @10:20    ->  10:20     alert if value >= 10 and value <= 20 (inverted)

    ``alert_inside`` is ``True`` only for the ``@``-prefixed inverted form.
    Returns ``None`` when *spec* is empty or cannot be parsed (the caller then
    treats the metric as having no threshold on that side).
    """
    if not spec:
        return None
    spec = spec.strip()
    if not spec:
        return None
    alert_inside = spec.startswith("@")
    if alert_inside:
        spec = spec[1:]
    if ":" in spec:
        start, end = spec.split(":", 1)
        low = _range_bound(start, empty=0.0, tilde=-math.inf)
        high = _range_bound(end, empty=math.inf, tilde=math.inf)
    else:
        low = 0.0
        high = _range_bound(spec, empty=math.inf, tilde=math.inf)
    if low is None or high is None:
        return None
    return (low, high, alert_inside)


def breaches_range(value: float | int | None, spec: str | None) -> bool:
    """Return ``True`` when *value* violates the Nagios threshold *spec*.

    ``False`` when *value* is ``None`` or *spec* is empty/unparseable -- an
    absent threshold can never be breached.
    """
    if value is None or not spec:
        return False
    parsed = parse_range(spec)
    if parsed is None:
        return False
    low, high, alert_inside = parsed
    if alert_inside:
        return low <= value <= high
    return value < low or value > high


def proximity_percent(value: float | int | None, spec: str | None) -> float | None:
    """How close *value* sits to breaching the threshold range *spec*, in percent.

    ``0.0`` means already breached; a small positive number means "almost
    breaching". The distance to the nearest *finite* alerting boundary is
    normalised by the range span (``high - low`` when both are finite, else the
    magnitude of the single finite boundary). Returns ``None`` when proximity is
    not computable (no value, no parseable range, or no finite boundary).

    Derived purely from the range, so it never assumes "higher = worse".
    """
    if value is None or not spec:
        return None
    parsed = parse_range(spec)
    if parsed is None:
        return None
    if breaches_range(value, spec):
        return 0.0
    low, high, alert_inside = parsed
    if alert_inside:
        # Value is outside the [low, high] alert band; distance to entering it.
        dist = (low - value) if value < low else (value - high)
        if math.isfinite(low) and math.isfinite(high):
            span = high - low
        else:
            finite = high if math.isfinite(high) else low
            span = abs(finite) if math.isfinite(finite) else 0.0
    else:
        # Value is inside the safe band; distance to the nearest alerting edge.
        dists: list[float] = []
        if math.isfinite(low):
            dists.append(value - low)
        if math.isfinite(high):
            dists.append(high - value)
        if not dists:
            return None
        dist = min(dists)
        if math.isfinite(low) and math.isfinite(high):
            span = high - low
        else:
            finite = high if math.isfinite(high) else low
            span = abs(finite) if math.isfinite(finite) else 0.0
    if span <= 0:
        return None
    return max(0.0, dist / span * 100.0)


def parse_metric(token: str) -> dict[str, Any] | None:
    """Parse a single ``label=value[UOM];warn;crit;min;max`` datum.

    Returns the structured metric dict, or ``None`` when *token* is not a
    recognisable perfdata datum (so :func:`parse_perfdata` can skip it).
    """
    match = _METRIC_RE.fullmatch(token.strip())
    if match is None:
        return None
    label = match.group("qlabel")
    if label is None:
        label = match.group("label")
    fields = match.group("rest").split(";")
    value_token = fields[0] if fields else ""

    value: float | int | None = None
    uom: str | None = None
    vmatch = _VALUE_RE.match(value_token.strip())
    if vmatch is not None:
        value = _num(vmatch.group("num"))
        uom = vmatch.group("uom").strip() or None

    def _field(idx: int) -> str | None:
        if idx < len(fields):
            raw = fields[idx].strip()
            return raw or None
        return None

    warn = _field(1)
    crit = _field(2)
    return {
        "label": label,
        "value": value,
        "uom": uom,
        "warn": warn,
        "crit": crit,
        "min": _num(fields[3]) if len(fields) > 3 else None,
        "max": _num(fields[4]) if len(fields) > 4 else None,
        "breached": breaches_range(value, warn) or breaches_range(value, crit),
    }


def parse_perfdata(perf_data: str | None) -> list[dict[str, Any]]:
    """Parse a full Nagios ``perf_data`` string into a list of metric dicts.

    Returns ``[]`` for an empty / ``None`` string and never raises -- malformed
    or partially-truncated tokens are skipped rather than aborting the parse.
    """
    if not perf_data:
        return []
    metrics: list[dict[str, Any]] = []
    for match in _METRIC_RE.finditer(perf_data):
        metric = parse_metric(match.group(0))
        if metric is not None:
            metrics.append(metric)
    return metrics
