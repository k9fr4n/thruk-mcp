"""Performance-data tools: expose host/service ``perf_data`` (issue #284).

Three read-only tools built purely on the already-available ``perf_data``
column of ``/hosts`` and ``/services`` -- no RRD / PNP / Grafana dependency
(time-series history is deferred to a follow-up):

* :func:`thruk_get_perfdata`            -- parsed metrics for one host/service.
* :func:`thruk_perfdata_snapshot`       -- parsed metrics for every service
  matching a structured filter (capacity planning).
* :func:`thruk_perfdata_near_threshold` -- metrics whose value is within N %% of
  breaching their warn/crit range (range-correct proximity).

The Nagios-spec parsing lives in :mod:`thruk_mcp.perfdata`; this module only
wires it to the Thruk REST client and the MCP tool registry.
"""

from __future__ import annotations

from typing import Any

from ..filters import (
    FIELDS_SERVICES,
    FilterError,
    build_tool_schema,
    compile_filter,
    validate_filter,
)
from ..helpers import _backends, _get_client, _seg, _tool_response
from ..perfdata import parse_perfdata, proximity_percent
from .base import _BACKENDS, _OPT_STR, ToolSpec, _int, _s, _str

#: Tight column set for the perfdata snapshot / near-threshold sweeps.
_PERF_SVC_COLUMNS = "host_name,description,perf_data"
#: Hard cap on rows scanned by the snapshot / near-threshold tools.
_PERF_MAX_LIMIT = 1000


def _svc_params(filter: dict[str, Any] | None, limit: int) -> dict[str, Any] | str:
    """Build /services query params for a perfdata sweep, or an error string."""
    params: dict[str, Any] = {
        "columns": _PERF_SVC_COLUMNS,
        "limit": max(1, min(limit, _PERF_MAX_LIMIT)),
    }
    if filter is not None:
        try:
            validate_filter(filter, FIELDS_SERVICES)
        except FilterError as exc:
            return str(exc)
        params.update(compile_filter(filter, "services"))
    return params


async def thruk_get_perfdata(
    host: str,
    service: str | None = None,
    backends: str | None = None,
) -> str:
    """Fetch and parse performance data for a single host or service.

    Reads the raw Nagios ``perf_data`` column and returns it as a structured
    list of metrics, so the agent can reason quantitatively ("disk C: is at
    77 %%, closest to its 90 %% warn threshold") instead of only OK/CRITICAL.

    * ``service=None`` -> the host check's perfdata (``/hosts/{host}``).
    * ``service="..."`` -> that service's perfdata (``/services/{host}/{svc}``).

    Each metric is::

        {"label", "value", "uom", "warn", "crit", "min", "max", "breached"}

    ``warn`` / ``crit`` are kept as raw Nagios range strings (e.g. ``-2000:2000``);
    ``breached`` is computed with real range semantics. An empty ``perf_data``
    yields ``metrics: []`` (never an error).
    """
    path = f"/services/{_seg(host)}/{_seg(service)}" if service else f"/hosts/{_seg(host)}"
    data = await _get_client().get(
        path, params={"columns": "perf_data"}, backends=_backends(backends)
    )
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    if not rows:
        target = f"Service {host!r}/{service!r}" if service else f"Host {host!r}"
        return _tool_response({"error": f"{target} not found"})
    first = rows[0] if isinstance(rows[0], dict) else {}
    metrics = parse_perfdata(first.get("perf_data") if isinstance(first, dict) else None)
    result: dict[str, Any] = {"host": host, "service": service, "metrics": metrics}
    warnings = None
    if len(rows) > 1:
        warnings = [
            f"{len(rows)} backends returned a result; parsed perf_data from the first only. "
            "Disambiguate with backends=."
        ]
    return _tool_response(result, warnings)


async def thruk_perfdata_snapshot(
    filter: dict[str, Any] | None = None,
    limit: int = 200,
    backends: str | None = None,
) -> str:
    """Parsed performance data for every service matching ``filter`` (one call).

    Use case: capacity planning -- "show CPU/RAM of every host in hostgroup
    PROD". Runs the structured AND/OR ``filter`` over ``/services``, pulls the
    ``perf_data`` column and returns ``{host, service, metrics:[...]}`` rows.

    ``filter`` fields: ``host``, ``description``, ``state``, ``hostgroup``,
    ``servicegroup``, ``custom_var``, ``host_custom_var`` (same contract as
    ``thruk_list_services``). ``limit`` caps the number of services scanned
    (max 1000). Services with empty ``perf_data`` come back with
    ``metrics: []``.
    """
    params = _svc_params(filter, limit)
    if isinstance(params, str):
        return _tool_response({"error": params})
    data = await _get_client().get("/services", params=params, backends=_backends(backends))
    rows = data if isinstance(data, list) else []
    results = [
        {
            "host": row.get("host_name"),
            "service": row.get("description"),
            "metrics": parse_perfdata(row.get("perf_data")),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    return _tool_response({"total": len(results), "results": results})


async def thruk_perfdata_near_threshold(
    filter: dict[str, Any] | None = None,
    within_percent: float = 10.0,
    limit: int = 200,
    backends: str | None = None,
) -> str:
    """Metrics within ``within_percent`` %% of breaching their warn/crit range.

    Answers "which services are within 10 %% of their threshold?". For every
    service matching ``filter``, each metric's proximity to its ``warn`` range
    (falling back to ``crit`` when no ``warn``) is computed *from the range*
    -- so inverted "lower is worse" metrics are handled correctly rather than
    assuming higher = worse. ``headroom_percent`` is ``0.0`` for an
    already-breached metric and grows as the value moves away from the edge.

    Returns the matching metrics flattened and sorted by ``headroom_percent``
    ascending (closest to breaching first). ``filter`` accepts the same fields
    as ``thruk_perfdata_snapshot``; ``limit`` caps services scanned (max 1000).
    """
    params = _svc_params(filter, limit)
    if isinstance(params, str):
        return _tool_response({"error": params})
    data = await _get_client().get("/services", params=params, backends=_backends(backends))
    rows = data if isinstance(data, list) else []
    matches: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        host = row.get("host_name")
        service = row.get("description")
        for metric in parse_perfdata(row.get("perf_data")):
            spec = metric["warn"] or metric["crit"]
            headroom = proximity_percent(metric["value"], spec)
            if headroom is None or headroom > within_percent:
                continue
            matches.append(
                {
                    "host": host,
                    "service": service,
                    "label": metric["label"],
                    "value": metric["value"],
                    "uom": metric["uom"],
                    "warn": metric["warn"],
                    "crit": metric["crit"],
                    "breached": metric["breached"],
                    "headroom_percent": round(headroom, 2),
                }
            )
    matches.sort(key=lambda m: m["headroom_percent"])
    return _tool_response(
        {"within_percent": within_percent, "total": len(matches), "results": matches}
    )


_WITHIN_PERCENT = {
    "type": "number",
    "default": 10.0,
    "description": (
        "Proximity threshold in percent. A metric is returned when its value is "
        "within this percentage of breaching its warn (or crit) range, or already "
        "breached (headroom 0)."
    ),
}
_PERF_LIMIT = _int("Maximum number of services to scan (max 1000).", default=200)

PERFDATA_REGISTRY: list[ToolSpec] = [
    ToolSpec(
        name="thruk_get_perfdata",
        fn=thruk_get_perfdata,
        schema=_s(
            "host",
            host=_str("Host name"),
            service={
                **_OPT_STR,
                "description": "Service description. Omit for the host check's own perfdata.",
            },
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_perfdata_snapshot",
        fn=thruk_perfdata_snapshot,
        schema=build_tool_schema(
            FIELDS_SERVICES,
            limit=_PERF_LIMIT,
            backends=_BACKENDS,
        ),
    ),
    ToolSpec(
        name="thruk_perfdata_near_threshold",
        fn=thruk_perfdata_near_threshold,
        schema=build_tool_schema(
            FIELDS_SERVICES,
            within_percent=_WITHIN_PERCENT,
            limit=_PERF_LIMIT,
            backends=_BACKENDS,
        ),
    ),
]

__all__ = [
    "PERFDATA_REGISTRY",
    "thruk_get_perfdata",
    "thruk_perfdata_near_threshold",
    "thruk_perfdata_snapshot",
]
