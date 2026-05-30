"""Write / command tools — Thruk external-command mutators (parent #256).

This module hosts the **mutating** tools (the ``WRITE_TOOLS`` block: read-only
mode strips them and the audit log records them) plus the lone read-only
``thruk_get_downtime`` lookup that closes the downtime CRUD loop:

* downtime scheduling — ``thruk_schedule_downtime``,
  ``thruk_schedule_host_services_downtime``,
  ``thruk_schedule_propagated_host_downtime``,
  ``thruk_schedule_hostgroup_downtime``,
  ``thruk_schedule_servicegroup_downtime``;
* downtime deletion / lookup — ``thruk_delete_downtime``,
  ``thruk_delete_active_downtimes``, ``thruk_delete_downtimes_by_filter``
  (+ the private ``_delete_downtimes_by_host_comment`` substring helper),
  ``thruk_get_downtime``;
* ack / comment / recheck / toggles — ``thruk_acknowledge``,
  ``thruk_bulk_acknowledge``, ``thruk_add_comment``, ``thruk_delete_comment``,
  ``thruk_remove_acknowledgement``, ``thruk_recheck``, ``thruk_checks``,
  ``thruk_notifications``.

This is a pure relocation out of :mod:`thruk_mcp.server` with **no behaviour
change**. Two co-located registries keep each tool's name, implementation and
explicit JSON Schema in one place:

* ``COMMANDS_READ_REGISTRY`` — the single read-only ``thruk_get_downtime``
  spec (``is_write=False``), spliced between the inventory and log/alert
  groups to preserve the original registration order;
* ``COMMANDS_WRITE_REGISTRY`` — the 16 mutating specs (all ``is_write=True``),
  spliced after the escape-hatch tools.

``server.py`` re-exports every symbol here for backward compatibility, so
existing imports (``from thruk_mcp.server import thruk_acknowledge, ...``)
keep working unchanged. Shared infrastructure (state maps, path/segment
encoding, downtime payload builder, peer resolution) lives in
:mod:`thruk_mcp.constants` / :mod:`thruk_mcp.helpers` so this module never
imports ``server``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from ..client import ThrukError
from ..constants import (
    HOST_STATE_INT,
    HOST_STATE_STR,
    SVC_STATE_INT,
    SVC_STATE_STR,
)
from ..helpers import (
    _backends,
    _downtime_payload,
    _get_client,
    _now_utc_epoch,
    _resolve_peer_for_host,
    _seg,
    _tool_response,
)
from .base import (
    _BACKENDS,
    _OPT_INT,
    _OPT_STR,
    ToolSpec,
    _bool,
    _int,
    _s,
    _str,
)


async def thruk_schedule_downtime(
    host: str,
    service: str | None = None,
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a host or service downtime (times accept 'now', relative '+2h'/'+30m', or ISO 8601).

    If `duration_minutes` is set it overrides `end_time`.

    Note: Naemon processes scheduling commands asynchronously through its
    command pipe. A newly scheduled downtime may not be immediately visible
    in Livestatus queries (`thruk_list_downtimes`, `thruk_delete_active_downtimes`,
    ...). Allow ~5-10 seconds before querying or deleting (issue #194)."""
    if duration_minutes:
        end_time = f"+{duration_minutes}m"
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/schedule_svc_downtime"
        if service
        else f"/hosts/{_seg(host)}/cmd/schedule_host_downtime"
    )
    payload = {
        "start_time": start_time,
        "end_time": end_time,
        "comment_data": comment,
        "comment_author": author,
        "fixed": "1" if fixed else "0",
    }
    return _tool_response(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends))
    )


async def thruk_acknowledge(
    host: str,
    service: str | None = None,
    comment: str = "acknowledged via MCP",
    author: str = "thruk-mcp",
    sticky: bool = True,
    notify: bool = True,
    persistent: bool = False,
    backends: str | None = None,
) -> str:
    """Acknowledge a host or service problem."""
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/acknowledge_svc_problem"
        if service
        else f"/hosts/{_seg(host)}/cmd/acknowledge_host_problem"
    )
    payload = {
        "comment_data": comment,
        "comment_author": author,
        "sticky_ack": "1" if sticky else "0",
        "send_notification": "1" if notify else "0",
        "persistent_comment": "1" if persistent else "0",
    }
    return _tool_response(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends))
    )


async def thruk_bulk_acknowledge(
    author: str = "thruk-mcp",
    comment: str = "bulk acknowledged via MCP",
    hostgroup: str | None = None,
    state: str | None = None,
    hosts_only: bool = False,
    services_only: bool = False,
    sticky: bool = True,
    notify: bool = True,
    persistent: bool = False,
    backends: str | None = None,
) -> str:
    """Acknowledge every unhandled problem matching the given filters in one call.

    Collects all currently unhandled (not acknowledged, not in downtime) host
    and/or service problems matching the optional ``hostgroup`` / ``state``
    filters, then fires every ``acknowledge_{host,svc}_problem`` POST
    concurrently via ``asyncio.gather``.

    Parameters:
    - ``state``: one of ``"down"``, ``"unreachable"`` (hosts) or
      ``"critical"``, ``"warning"``, ``"unknown"`` (services). ``None`` =
      every non-OK state.  Numeric strings ("0".."3") are also accepted via
      the canonical state-int maps.
    - ``hosts_only``: skip service problems entirely.
    - ``services_only``: skip host problems entirely.
    - ``hostgroup``: restrict to members of this hostgroup (resolved via
      Livestatus ``groups[gte]`` / ``host_groups[gte]`` — same semantics as
      ``thruk_problems``).
    - ``sticky`` / ``notify`` / ``persistent``: forwarded verbatim to
      ``acknowledge_*_problem`` (payload keys ``sticky_ack``,
      ``send_notification``, ``persistent_comment``).

    Returns a JSON summary:

    .. code-block:: json

        {
          "acknowledged": 12,
          "failed": 0,
          "targets": [{"host": "srv01", "service": null, "state": "DOWN"}, ...],
          "errors": []
        }

    When zero targets match, returns ``acknowledged=0`` plus a ``_warning``
    note — it is not an error.
    """
    if hosts_only and services_only:
        return _tool_response({"error": "hosts_only and services_only are mutually exclusive"})

    # Resolve state filter to host / service int (None = any non-OK).
    host_state_int: int | None = None
    svc_state_int: int | None = None
    skip_hosts = services_only
    skip_services = hosts_only
    if state is not None:
        key = state.lower()
        if key in HOST_STATE_INT and HOST_STATE_INT[key] != 0:
            host_state_int = HOST_STATE_INT[key]
            skip_services = True  # host-only state
        elif key in SVC_STATE_INT and SVC_STATE_INT[key] != 0:
            svc_state_int = SVC_STATE_INT[key]
            skip_hosts = True  # service-only state
        else:
            return _tool_response(
                {
                    "error": (
                        f"invalid state {state!r}: expected one of "
                        "down, unreachable, critical, warning, unknown"
                    )
                }
            )

    be = _backends(backends)

    async def _collect_hosts() -> list[dict[str, Any]]:
        if skip_hosts:
            return []
        params: dict[str, Any] = {
            "acknowledged": 0,
            "scheduled_downtime_depth": 0,
            "columns": "name,state,peer_name",
        }
        if host_state_int is not None:
            params["state"] = host_state_int
        else:
            params["state[gte]"] = 1
        if hostgroup:
            params["groups[gte]"] = hostgroup
        rows: list[dict[str, Any]] = []
        async for row in _get_client().get_all("/hosts", params=params, backends=be):
            if isinstance(row, dict) and row.get("name"):
                rows.append(row)
        return rows

    async def _collect_services() -> list[dict[str, Any]]:
        if skip_services:
            return []
        params: dict[str, Any] = {
            "acknowledged": 0,
            "scheduled_downtime_depth": 0,
            "columns": "host_name,description,state,peer_name",
        }
        if svc_state_int is not None:
            params["state"] = svc_state_int
        else:
            params["state[gte]"] = 1
        if hostgroup:
            params["host_groups[gte]"] = hostgroup
        rows: list[dict[str, Any]] = []
        async for row in _get_client().get_all("/services", params=params, backends=be):
            if isinstance(row, dict) and row.get("host_name") and row.get("description"):
                rows.append(row)
        return rows

    hosts, services = await asyncio.gather(_collect_hosts(), _collect_services())

    targets: list[dict[str, Any]] = []
    coros: list[Coroutine[Any, Any, Any]] = []
    payload = {
        "comment_data": comment,
        "comment_author": author,
        "sticky_ack": "1" if sticky else "0",
        "send_notification": "1" if notify else "0",
        "persistent_comment": "1" if persistent else "0",
    }

    for h in hosts:
        name = str(h.get("name", ""))
        targets.append(
            {
                "host": name,
                "service": None,
                "state": HOST_STATE_STR.get(int(h.get("state", -1)), str(h.get("state", ""))),
            }
        )
        coros.append(
            _get_client().post(
                f"/hosts/{_seg(name)}/cmd/acknowledge_host_problem",
                data=payload,
                backends=be,
            )
        )
    for s in services:
        h_name = str(s.get("host_name", ""))
        svc = str(s.get("description", ""))
        targets.append(
            {
                "host": h_name,
                "service": svc,
                "state": SVC_STATE_STR.get(int(s.get("state", -1)), str(s.get("state", ""))),
            }
        )
        coros.append(
            _get_client().post(
                f"/services/{_seg(h_name)}/{_seg(svc)}/cmd/acknowledge_svc_problem",
                data=payload,
                backends=be,
            )
        )

    result: dict[str, Any] = {
        "acknowledged": 0,
        "failed": 0,
        "targets": targets,
        "errors": [],
    }

    if not coros:
        result["_warning"] = "no matching unhandled problems found — nothing to acknowledge"
        return _tool_response(result)

    results = await asyncio.gather(*coros, return_exceptions=True)
    errors: list[dict[str, Any]] = []
    ok_count = 0
    for tgt, res in zip(targets, results, strict=True):
        if isinstance(res, Exception):
            errors.append({**tgt, "error": str(res)})
        else:
            ok_count += 1
    result["acknowledged"] = ok_count
    result["failed"] = len(errors)
    result["errors"] = errors
    return _tool_response(result)


async def thruk_add_comment(
    host: str,
    comment: str,
    service: str | None = None,
    author: str = "thruk-mcp",
    persistent: bool = True,
    backends: str | None = None,
) -> str:
    """Add a free-form operator comment on a host or service.

    Posts a timestamped note via Thruk REST without acknowledging the problem
    or scheduling a downtime.  Typical use-cases: incident timeline annotations
    ("Investigating high load, ETA 30 min"), false-positive markers, ops handoff
    notes.

    Thruk commands used:
    - host:    ``POST /hosts/{host}/cmd/add_host_comment``
    - service: ``POST /services/{host}/{svc}/cmd/add_svc_comment``

    Payload keys forwarded to Thruk:
    - ``comment_data``   — the comment text
    - ``comment_author`` — display name of the author
    - ``persistent``     — when "1" the comment survives Nagios restarts /
      subsequent check results; when "0" it is dropped on the next check.

    This tool does **not** acknowledge a problem (use ``thruk_acknowledge``)
    and does **not** schedule a downtime (use ``thruk_schedule_downtime``).
    """
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/add_svc_comment"
        if service
        else f"/hosts/{_seg(host)}/cmd/add_host_comment"
    )
    payload = {
        "comment_data": comment,
        "comment_author": author,
        "persistent": "1" if persistent else "0",
    }
    return _tool_response(
        await _get_client().post(endpoint, data=payload, backends=_backends(backends))
    )


async def thruk_delete_comment(
    comment_id: int,
    host: str,
    service: str | None = None,
    backends: str | None = None,
) -> str:
    """Delete a host or service comment by its id.

    Closes the CRUD loop for operator notes: ``thruk_list_comments`` exposes
    comment ids, ``thruk_add_comment`` creates them, and this tool deletes
    them.  Typical use-cases:

    - remove a stale investigation note after the incident is resolved,
    - clean up comments created by an LLM assistant during an incident.

    Thruk commands used (the command-based path is selected because the REST
    ``DELETE /comments/{id}`` endpoint is not guaranteed across Thruk
    versions):

    - host:    ``POST /hosts/{host}/cmd/del_host_comment``
    - service: ``POST /services/{host}/{svc}/cmd/del_svc_comment``

    Payload key forwarded to Thruk:

    - ``comment_id`` — the numeric id returned by ``thruk_list_comments``.

    ``host`` is required so Thruk can route the command to the correct
    backend.  ``service`` selects the service-scoped command path; omit it
    for host comments.
    """
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/del_svc_comment"
        if service
        else f"/hosts/{_seg(host)}/cmd/del_host_comment"
    )
    return _tool_response(
        await _get_client().post(
            endpoint, data={"comment_id": str(comment_id)}, backends=_backends(backends)
        )
    )


async def thruk_remove_acknowledgement(
    host: str, service: str | None = None, backends: str | None = None
) -> str:
    """Remove an acknowledgement."""
    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/remove_svc_acknowledgement"
        if service
        else f"/hosts/{_seg(host)}/cmd/remove_host_acknowledgement"
    )
    return _tool_response(await _get_client().post(endpoint, backends=_backends(backends)))


async def thruk_recheck(
    host: str, service: str | None = None, forced: bool = True, backends: str | None = None
) -> str:
    """Schedule an immediate (re)check for a host or service."""
    if service:
        cmd = "schedule_forced_svc_check" if forced else "schedule_svc_check"
        endpoint = f"/services/{_seg(host)}/{_seg(service)}/cmd/{cmd}"
    else:
        cmd = "schedule_forced_host_check" if forced else "schedule_host_check"
        endpoint = f"/hosts/{_seg(host)}/cmd/{cmd}"
    return _tool_response(
        await _get_client().post(endpoint, data={"start_time": "now"}, backends=_backends(backends))
    )


async def thruk_notifications(
    host: str,
    enabled: bool,
    service: str | None = None,
    cascade: bool = False,
    backends: str | None = None,
) -> str:
    """Enable or disable notifications for a host or service.

    ``enabled=True``  → enable notifications.
    ``enabled=False`` → disable notifications.

    When ``service`` is omitted the command targets the host itself.
    Set ``cascade=True`` to also apply the same command to **all services**
    of the host (ignored when ``service`` is specified).

    Thruk commands used:
    - host:    ``enable_host_notifications`` / ``disable_host_notifications``
    - service: ``enable_svc_notifications``  / ``disable_svc_notifications``

    This tool does **not** schedule a downtime and does **not** acknowledge
    any problem — it only controls whether Thruk sends out alerts.
    """
    client = _get_client()
    be = _backends(backends)
    results: list[Any] = []

    if service:
        # Single service — cascade is irrelevant
        verb = "enable_svc_notifications" if enabled else "disable_svc_notifications"
        endpoint = f"/services/{_seg(host)}/{_seg(service)}/cmd/{verb}"
        results.append(await client.post(endpoint, backends=be))
    else:
        # Host-level command
        verb_host = "enable_host_notifications" if enabled else "disable_host_notifications"
        results.append(await client.post(f"/hosts/{_seg(host)}/cmd/{verb_host}", backends=be))

        if cascade:
            # Apply to every service of this host
            verb_svc = "enable_svc_notifications" if enabled else "disable_svc_notifications"
            svc_data = await client.get(
                f"/hosts/{_seg(host)}/services",
                params={"columns": "description"},
                backends=be,
            )
            services: list[str] = []
            if isinstance(svc_data, list):
                services = [
                    s["description"]
                    for s in svc_data
                    if isinstance(s, dict) and s.get("description")
                ]
            svc_results = await asyncio.gather(
                *(
                    client.post(f"/services/{_seg(host)}/{_seg(svc)}/cmd/{verb_svc}", backends=be)
                    for svc in services
                )
            )
            results.extend(svc_results)

    action = "enabled" if enabled else "disabled"
    target = f"{host}/{service}" if service else host
    if cascade and not service:
        target = f"{host} (host + all services)"
    return _tool_response({"action": action, "target": target, "results": results})


async def thruk_checks(
    host: str,
    enabled: bool,
    service: str | None = None,
    cascade: bool = False,
    backends: str | None = None,
) -> str:
    """Enable or disable active checks for a host or service.

    ``enabled=True``  → enable active checks.
    ``enabled=False`` → disable active checks.

    When ``service`` is omitted the command targets the host itself.
    Set ``cascade=True`` to also apply the same command to **all services**
    of the host (ignored when ``service`` is specified).

    Thruk commands used:
    - host:    ``enable_host_checks`` / ``disable_host_checks``
    - service: ``enable_svc_checks``  / ``disable_svc_checks``

    This tool does **not** schedule a downtime and does **not** acknowledge
    any problem — it only controls whether Thruk runs active checks. Passive
    check submissions are unaffected.
    """
    client = _get_client()
    be = _backends(backends)
    results: list[Any] = []

    if service:
        # Single service — cascade is irrelevant
        verb = "enable_svc_checks" if enabled else "disable_svc_checks"
        endpoint = f"/services/{_seg(host)}/{_seg(service)}/cmd/{verb}"
        results.append(await client.post(endpoint, backends=be))
    else:
        # Host-level command
        verb_host = "enable_host_checks" if enabled else "disable_host_checks"
        results.append(await client.post(f"/hosts/{_seg(host)}/cmd/{verb_host}", backends=be))

        if cascade:
            # Apply to every service of this host
            verb_svc = "enable_svc_checks" if enabled else "disable_svc_checks"
            svc_data = await client.get(
                f"/hosts/{_seg(host)}/services",
                params={"columns": "description"},
                backends=be,
            )
            services: list[str] = []
            if isinstance(svc_data, list):
                services = [
                    s["description"]
                    for s in svc_data
                    if isinstance(s, dict) and s.get("description")
                ]
            svc_results = await asyncio.gather(
                *(
                    client.post(f"/services/{_seg(host)}/{_seg(svc)}/cmd/{verb_svc}", backends=be)
                    for svc in services
                )
            )
            results.extend(svc_results)

    action = "enabled" if enabled else "disabled"
    target = f"{host}/{service}" if service else host
    if cascade and not service:
        target = f"{host} (host + all services)"
    return _tool_response({"action": action, "target": target, "results": results})


async def thruk_delete_downtime(
    downtime_id: int, host: str, service: str | None = None, backends: str | None = None
) -> str:
    """Delete a host or service downtime by its id.

    If `service` is omitted, the tool fetches the downtime object first
    (`GET /downtimes/{id}`) to determine whether it belongs to a host or a
    service, then routes to the correct Thruk REST endpoint
    (`/hosts/.../cmd/del_downtime` vs `/services/.../cmd/del_downtime`).
    Providing `service` explicitly skips that extra round-trip.
    """
    client = _get_client()
    be = _backends(backends)

    # Auto-detect downtime type when service is not provided to avoid silently
    # hitting the host endpoint on a service downtime (no-op with misleading
    # "Command successfully submitted" response — see issue #35).
    if service is None:
        dt = await client.get(f"/downtimes/{_seg(str(downtime_id))}", backends=be)
        svc_desc = dt.get("service_description") if isinstance(dt, dict) else None
        service = svc_desc or None

    endpoint = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/del_downtime"
        if service
        else f"/hosts/{_seg(host)}/cmd/del_downtime"
    )
    return _tool_response(
        await client.post(endpoint, data={"downtime_id": str(downtime_id)}, backends=be)
    )


async def thruk_get_downtime(downtime_id: int, backends: str | None = None) -> str:
    """Get a single downtime by id.

    The Thruk REST ``/downtimes/{id}`` endpoint always returns a JSON list
    (one entry per backend in a federated setup). This tool unpacks that
    list so callers get the expected single object, mirroring
    ``thruk_get_host`` / ``thruk_get_service``:

    - empty list  -> ``{"error": "Downtime <id> not found"}``
    - one entry   -> the dict itself
    - many entries (same downtime id on multiple backends) -> the list,
      with a ``_warnings`` entry flagging the collision so the caller can
      disambiguate via ``backends=``.
    """
    data = await _get_client().get(
        f"/downtimes/{_seg(str(downtime_id))}", backends=_backends(backends)
    )
    if not isinstance(data, list):
        return _tool_response(data)
    if not data:
        return _tool_response({"error": f"Downtime {downtime_id} not found"})
    if len(data) == 1:
        return _tool_response(data[0])
    return _tool_response(
        data,
        [f"{len(data)} backends returned a result for downtime {downtime_id}; listing all."],
    )


async def thruk_schedule_host_services_downtime(
    host: str,
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime on ALL services of the given host (not the host object itself).

    Uses schedule_host_svc_downtime. Use thruk_schedule_downtime for the host
    itself or for one specific service.

    Note: Naemon processes scheduling commands asynchronously; new downtimes
    may not be immediately visible in Livestatus (issue #194)."""
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return _tool_response(
        await _get_client().post(
            f"/hosts/{_seg(host)}/cmd/schedule_host_svc_downtime",
            data=payload,
            backends=_backends(backends),
        )
    )


async def thruk_schedule_propagated_host_downtime(
    host: str,
    triggered: bool = False,
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime on a host and propagate to all child hosts.
    If `triggered=True`, child downtimes are triggered by the parent (start
    when the parent enters its downtime). Useful for a parent network
    device whose children should automatically follow."""
    cmd = (
        "schedule_and_propagate_triggered_host_downtime"
        if triggered
        else "schedule_and_propagate_host_downtime"
    )
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return _tool_response(
        await _get_client().post(
            f"/hosts/{_seg(host)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        )
    )


async def thruk_schedule_hostgroup_downtime(
    hostgroup: str,
    target: str = "hosts",
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime on every host or service of a hostgroup.

    `target='hosts'` (default) covers the group's hosts; `target='services'`
    covers their services."""
    cmd = (
        "schedule_hostgroup_svc_downtime"
        if target == "services"
        else "schedule_hostgroup_host_downtime"
    )
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return _tool_response(
        await _get_client().post(
            f"/hostgroups/{_seg(hostgroup)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        )
    )


async def thruk_schedule_servicegroup_downtime(
    servicegroup: str,
    target: str = "services",
    comment: str = "requested via MCP",
    author: str = "thruk-mcp",
    start_time: str = "now",
    end_time: str = "+2h",
    duration_minutes: int | None = None,
    fixed: bool = True,
    backends: str | None = None,
) -> str:
    """Schedule a downtime on a servicegroup's services or owning hosts.

    `target='services'` (default) targets all services in the group;
    `target='hosts'` targets the hosts owning those services."""
    cmd = (
        "schedule_servicegroup_host_downtime"
        if target == "hosts"
        else "schedule_servicegroup_svc_downtime"
    )
    payload = _downtime_payload(comment, author, start_time, end_time, duration_minutes, fixed, 0)
    return _tool_response(
        await _get_client().post(
            f"/servicegroups/{_seg(servicegroup)}/cmd/{cmd}",
            data=payload,
            backends=_backends(backends),
        )
    )


async def thruk_delete_active_downtimes(
    host: str,
    service: str | None = None,
    backends: str | None = None,
    retry_on_empty: bool = True,
    retry_delay_seconds: float = 2.0,
) -> str:
    """Remove ALL currently active downtimes for a host (or one specific service).

    Pass `service` to scope to a single service. Fetches all active downtime
    IDs first, then submits one DEL_*_DOWNTIME per ID. Partial failures are
    reported individually in `errors` instead of aborting the whole batch.

    Naemon processes scheduling commands asynchronously through its command
    pipe (issue #194): a downtime created by ``thruk_schedule_downtime`` /
    ``thruk_schedule_host_services_downtime`` may not be visible in
    Livestatus for a few seconds. When the initial ``/downtimes`` lookup
    returns zero matches and ``retry_on_empty=True`` (the default), the
    tool waits ``retry_delay_seconds`` and re-queries once. If still empty,
    the response includes a structured ``_warning`` so callers can detect
    the lag instead of assuming there is nothing to delete."""
    client = _get_client()
    be = _backends(backends)

    # Query active downtimes: started and not yet ended (same logic as thruk_list_downtimes).
    def _build_params() -> dict[str, Any]:
        p: dict[str, Any] = {
            "host_name": host,
            "start_time[lte]": _now_utc_epoch(),
            "end_time[gte]": _now_utc_epoch(),
            "columns": "id,service_description,author,comment",
        }
        if service:
            p["service_description"] = service
        return p

    async def _fetch_matching() -> list[dict[str, Any]]:
        raw = await client.get("/downtimes", params=_build_params(), backends=be)
        all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])
        # Keep only the right type: host-level (empty service_desc) or the requested service.
        if service:
            return [d for d in all_dts if d.get("service_description") == service]
        return [d for d in all_dts if not d.get("service_description")]

    downtimes = await _fetch_matching()

    # Issue #194: Naemon command pipe is async — a freshly-scheduled downtime
    # may not yet be visible in Livestatus. Retry once after a short backoff
    # before giving up, unless the caller explicitly opts out.
    if not downtimes and retry_on_empty and retry_delay_seconds > 0:
        await asyncio.sleep(retry_delay_seconds)
        downtimes = await _fetch_matching()

    if not downtimes:
        return _tool_response(
            {
                "deleted": [],
                "errors": [],
                "count": 0,
                "message": "No active downtimes found.",
                "_warning": (
                    "No active downtimes visible in Livestatus. Naemon processes "
                    "scheduling commands asynchronously through its command pipe — "
                    "if a downtime was just created, retry in a few seconds. "
                    "See issue #194."
                ),
            }
        )

    # Thruk REST exposes only `del_downtime` (not `del_svc_downtime` /
    # `del_host_downtime`) — the correct Nagios external command is inferred
    # from the resource path (issue #36).
    ep = (
        f"/services/{_seg(host)}/{_seg(service)}/cmd/del_downtime"
        if service
        else f"/hosts/{_seg(host)}/cmd/del_downtime"
    )

    # Issue #141: parallelise all per-id DEL requests via asyncio.gather so that
    # N downtimes complete in ~1 RTT instead of N RTTs.  Error isolation is
    # preserved: each coroutine catches ThrukError and returns it as a value so
    # that a failure on one id never aborts the rest.
    async def _del_one(dt_id: int) -> tuple[int, Any, ThrukError | None]:
        try:
            resp = await client.post(ep, data={"downtime_id": dt_id}, backends=be)
            return dt_id, resp, None
        except ThrukError as exc:
            return dt_id, None, exc

    ids = [d["id"] for d in downtimes if d.get("id") is not None]
    _gather_results: list[tuple[int, Any, ThrukError | None]] = list(
        await asyncio.gather(*(_del_one(i) for i in ids))
    )
    deleted = [{"downtime_id": i, "result": r} for i, r, e in _gather_results if e is None]
    errors = [{"downtime_id": i, "error": str(e)} for i, _, e in _gather_results if e is not None]

    return _tool_response({"deleted": deleted, "errors": errors, "count": len(deleted)})


async def thruk_delete_downtimes_by_filter(
    host: str | None = None,
    hostgroup: str | None = None,
    service: str | None = None,
    start_time: str | None = None,
    comment: str | None = None,
    backends: str | None = None,
) -> str:
    """Bulk-delete downtimes matching arbitrary filters.

    Strategy depends on the filter combination:

    * **``host`` + ``comment``** (issue #197): the tool enumerates downtimes
      for the host via ``/downtimes`` and applies a **case-insensitive
      substring** match on ``comment`` client-side, then issues per-id
      ``del_downtime`` commands against the matching host- or service-level
      endpoint. This avoids Naemon's exact-string comparison on the comment
      field (``DEL_DOWNTIME_BY_HOST_NAME`` would otherwise silently no-op for
      partial-comment filters). Matches are reported under
      ``host_downtimes_*`` and ``service_downtimes_*``.
    * **``host`` only**: bulk via ``del_downtime_by_host_name`` system command
      (service downtimes) plus explicit enumeration of host-level downtimes
      (which the system command does not cover).
    * **``hostgroup``**: bulk via ``del_downtime_by_hostgroup_name``.
    * **``comment`` or ``start_time`` only**: bulk via
      ``del_downtime_by_start_time_comment`` — **exact** match on ``comment``
      (Naemon limitation, no client-side fallback available).

    At least one of ``host``, ``hostgroup``, ``service``, ``start_time`` or
    ``comment`` must be provided."""
    client = _get_client()
    be = _backends(backends)

    if not any([host, hostgroup, service, start_time, comment]):
        raise ThrukError("Provide at least one of host, hostgroup, service, start_time, comment.")

    # Issue #196: when filtering by host without an explicit `backends=`
    # override, pre-resolve the backend owning the host so commands are not
    # broadcast to every Naemon site (11/12 useless commands in a typical
    # federation). Ambiguous lookups fall back to broadcast.
    if host and not hostgroup and be is None:
        resolved = await _resolve_peer_for_host(client, host)
        if resolved is not None:
            be = resolved

    # ------------------------------------------------------------------
    # Issue #197: host + comment → client-side substring match path.
    # Skip the system command entirely (its comment match is exact and
    # silently no-ops for partial filters).
    # ------------------------------------------------------------------
    if host and not hostgroup and comment:
        return await _delete_downtimes_by_host_comment(
            client, be, host=host, comment=comment, service=service, start_time=start_time
        )

    # ------------------------------------------------------------------
    # Bulk system-command path (no client-side filtering available).
    # ------------------------------------------------------------------
    payload: dict[str, str] = {}
    if host:
        payload["hostname"] = host
    if hostgroup:
        payload["hostgroup_name"] = hostgroup
    if service:
        payload["service_desc"] = service
    if start_time:
        payload["start_time"] = start_time
    if comment:
        # NOTE: exact-match only — see docstring (issue #197).
        payload["comment"] = comment

    if hostgroup:
        cmd = "del_downtime_by_hostgroup_name"
    elif host:
        cmd = "del_downtime_by_host_name"
    else:
        cmd = "del_downtime_by_start_time_comment"

    cmd_result = await client.post(f"/system/cmd/{cmd}", data=payload, backends=be)
    result: dict[str, Any] = {"system_command": cmd_result}

    # DEL_DOWNTIME_BY_HOST_NAME (Naemon) only targets service downtimes.
    # Enumerate + delete host-level downtimes explicitly when filtering by host.
    if host and not hostgroup:
        dt_params: dict[str, Any] = {
            "host_name": host,
            "columns": "id,service_description,comment,start_time",
        }
        if start_time:
            dt_params["start_time"] = start_time

        raw = await client.get("/downtimes", params=dt_params, backends=be)
        all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])
        # Host-level downtimes have an empty service_description.
        host_dts = [d for d in all_dts if not d.get("service_description")]

        # Issue #141: parallelise per-id DEL via asyncio.gather.
        async def _del_host_one(dt_id: int) -> tuple[int, Any, ThrukError | None]:
            try:
                resp = await client.post(
                    f"/hosts/{_seg(host)}/cmd/del_downtime",
                    data={"downtime_id": dt_id},
                    backends=be,
                )
                return dt_id, resp, None
            except ThrukError as exc:
                return dt_id, None, exc

        host_ids = [d["id"] for d in host_dts if d.get("id") is not None]
        _host_gather: list[tuple[int, Any, ThrukError | None]] = list(
            await asyncio.gather(*(_del_host_one(i) for i in host_ids))
        )
        host_deleted = [{"downtime_id": i, "result": r} for i, r, e in _host_gather if e is None]
        host_errors = [
            {"downtime_id": i, "error": str(e)} for i, _, e in _host_gather if e is not None
        ]

        result["host_downtimes_deleted"] = host_deleted
        result["host_downtimes_errors"] = host_errors

    return _tool_response(result)


async def _delete_downtimes_by_host_comment(
    client: Any,
    be: tuple[str, ...] | None,
    *,
    host: str,
    comment: str,
    service: str | None,
    start_time: str | None,
) -> str:
    """Issue #197: client-side substring filter on the ``comment`` field.

    Enumerates downtimes for ``host`` (optionally narrowed by ``service`` and
    ``start_time``), keeps only those whose comment contains ``comment``
    (case-insensitive), then issues per-id ``del_downtime`` against the
    correct endpoint (host- vs service-level) in parallel via
    :func:`asyncio.gather`.

    This works around Naemon's exact-string comparison on the comment field
    in ``DEL_DOWNTIME_BY_HOST_NAME`` which would silently no-op on partial
    matches and return ``{"message": "Command successfully submitted"}``."""
    dt_params: dict[str, Any] = {
        "host_name": host,
        "columns": "id,service_description,comment,start_time",
    }
    if service:
        dt_params["service_description"] = service
    if start_time:
        dt_params["start_time"] = start_time

    raw = await client.get("/downtimes", params=dt_params, backends=be)
    all_dts: list[dict[str, Any]] = raw if isinstance(raw, list) else ([raw] if raw else [])

    needle = comment.lower()
    matching = [d for d in all_dts if needle in str(d.get("comment", "")).lower()]
    host_dts = [d for d in matching if not d.get("service_description")]
    svc_dts = [d for d in matching if d.get("service_description")]

    async def _del_one(endpoint: str, dt_id: int) -> tuple[int, Any, ThrukError | None]:
        try:
            resp = await client.post(endpoint, data={"downtime_id": dt_id}, backends=be)
            return dt_id, resp, None
        except ThrukError as exc:
            return dt_id, None, exc

    host_ep = f"/hosts/{_seg(host)}/cmd/del_downtime"
    host_coros = [_del_one(host_ep, d["id"]) for d in host_dts if d.get("id") is not None]
    svc_coros = [
        _del_one(
            f"/services/{_seg(host)}/{_seg(str(d['service_description']))}/cmd/del_downtime",
            d["id"],
        )
        for d in svc_dts
        if d.get("id") is not None
    ]

    host_results: list[tuple[int, Any, ThrukError | None]] = list(await asyncio.gather(*host_coros))
    svc_results: list[tuple[int, Any, ThrukError | None]] = list(await asyncio.gather(*svc_coros))

    def _split(
        rows: list[tuple[int, Any, ThrukError | None]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ok_rows = [{"downtime_id": i, "result": r} for i, r, e in rows if e is None]
        err_rows = [{"downtime_id": i, "error": str(e)} for i, _, e in rows if e is not None]
        return ok_rows, err_rows

    host_ok, host_err = _split(host_results)
    svc_ok, svc_err = _split(svc_results)

    return _tool_response(
        {
            "match_mode": "substring",
            "comment_substring": comment,
            "matched": len(matching),
            "host_downtimes_deleted": host_ok,
            "host_downtimes_errors": host_err,
            "service_downtimes_deleted": svc_ok,
            "service_downtimes_errors": svc_err,
        }
    )


COMMANDS_READ_REGISTRY: list[ToolSpec] = [
    ToolSpec(
        name="thruk_get_downtime",
        fn=thruk_get_downtime,
        schema=_s("downtime_id", downtime_id=_int(), backends=_BACKENDS),
    ),
]


COMMANDS_WRITE_REGISTRY: list[ToolSpec] = [
    # ---------------------------------------------------------------- write: downtime scheduling
    ToolSpec(
        name="thruk_schedule_downtime",
        fn=thruk_schedule_downtime,
        schema=_s(
            "host",
            host=_str("Host name"),
            service=_OPT_STR,
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_host_services_downtime",
        fn=thruk_schedule_host_services_downtime,
        schema=_s(
            "host",
            host=_str("Host name"),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_propagated_host_downtime",
        fn=thruk_schedule_propagated_host_downtime,
        schema=_s(
            "host",
            host=_str("Host name"),
            triggered=_bool(default=False),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_hostgroup_downtime",
        fn=thruk_schedule_hostgroup_downtime,
        schema=_s(
            "hostgroup",
            hostgroup=_str("Hostgroup name"),
            target=_str(),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_schedule_servicegroup_downtime",
        fn=thruk_schedule_servicegroup_downtime,
        schema=_s(
            "servicegroup",
            servicegroup=_str("Servicegroup name"),
            target=_str(),
            comment=_str(),
            author=_str(),
            start_time=_str(),
            end_time=_str(),
            duration_minutes=_OPT_INT,
            fixed=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    # ---------------------------------------------------------------- write: downtime deletion
    ToolSpec(
        name="thruk_delete_downtime",
        fn=thruk_delete_downtime,
        schema=_s(
            "downtime_id",
            "host",
            downtime_id=_int(),
            host=_str(),
            service=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_delete_active_downtimes",
        fn=thruk_delete_active_downtimes,
        schema=_s(
            "host",
            host=_str(),
            service=_OPT_STR,
            backends=_BACKENDS,
            retry_on_empty=_bool(
                desc=(
                    "Retry the /downtimes lookup once after a short delay if the first "
                    "query returns no matches. Works around Naemon's async command pipe "
                    "(issue #194). Default: True."
                ),
                default=True,
            ),
            retry_delay_seconds={
                "type": "number",
                "default": 2.0,
                "description": (
                    "Seconds to wait before the retry when retry_on_empty=True. "
                    "Set to 0 to disable the wait. Default: 2.0."
                ),
            },
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_delete_downtimes_by_filter",
        fn=thruk_delete_downtimes_by_filter,
        schema=_s(
            host=_OPT_STR,
            hostgroup=_OPT_STR,
            service=_OPT_STR,
            start_time=_OPT_STR,
            comment=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    # ---------------------------------------------------------------- write: ack / recheck
    ToolSpec(
        name="thruk_acknowledge",
        fn=thruk_acknowledge,
        schema=_s(
            "host",
            host=_str("Host name"),
            service=_OPT_STR,
            comment=_str(),
            author=_str(),
            sticky=_bool(default=True),
            notify=_bool(default=True),
            persistent=_bool(default=False),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_bulk_acknowledge",
        fn=thruk_bulk_acknowledge,
        schema=_s(
            author=_str(),
            comment=_str(),
            hostgroup=_OPT_STR,
            state={
                **_OPT_STR,
                "description": (
                    "Restrict to a single state: 'down' / 'unreachable' (hosts) or "
                    "'critical' / 'warning' / 'unknown' (services). "
                    "None (default) matches every non-OK problem."
                ),
            },
            hosts_only=_bool(default=False),
            services_only=_bool(default=False),
            sticky=_bool(default=True),
            notify=_bool(default=True),
            persistent=_bool(default=False),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_add_comment",
        fn=thruk_add_comment,
        schema=_s(
            "host",
            "comment",
            host=_str("Host name"),
            comment=_str("Free-form comment text to attach to the host or service."),
            service=_OPT_STR,
            author=_str(),
            persistent=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_delete_comment",
        fn=thruk_delete_comment,
        schema=_s(
            "comment_id",
            "host",
            comment_id=_int("Numeric comment id (as returned by thruk_list_comments)."),
            host=_str("Host name owning the comment."),
            service=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_remove_acknowledgement",
        fn=thruk_remove_acknowledgement,
        schema=_s(
            "host",
            host=_str(),
            service=_OPT_STR,
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_recheck",
        fn=thruk_recheck,
        schema=_s(
            "host",
            host=_str("Host name"),
            service=_OPT_STR,
            forced=_bool(default=True),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_checks",
        fn=thruk_checks,
        schema=_s(
            "host",
            "enabled",
            host=_str("Host name"),
            enabled=_bool(
                "True to enable active checks, False to disable.",
            ),
            service={
                **_OPT_STR,
                "description": (
                    "Service description. Omit to target the host only "
                    "(use cascade=true to also cover all its services)."
                ),
            },
            cascade=_bool(
                "When true and no service is specified, also apply to all services "
                "of the host. Ignored when service is set.",
                default=False,
            ),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
    ToolSpec(
        name="thruk_notifications",
        fn=thruk_notifications,
        schema=_s(
            "host",
            "enabled",
            host=_str("Host name"),
            enabled=_bool(
                "True to enable notifications, False to disable.",
            ),
            service={
                **_OPT_STR,
                "description": (
                    "Service description. Omit to target the host only "
                    "(use cascade=true to also cover all its services)."
                ),
            },
            cascade=_bool(
                "When true and no service is specified, also apply to all services "
                "of the host. Ignored when service is set.",
                default=False,
            ),
            backends=_BACKENDS,
        ),
        is_write=True,
    ),
]


__all__ = [
    "COMMANDS_READ_REGISTRY",
    "COMMANDS_WRITE_REGISTRY",
    "_delete_downtimes_by_host_comment",
    "thruk_acknowledge",
    "thruk_add_comment",
    "thruk_bulk_acknowledge",
    "thruk_checks",
    "thruk_delete_active_downtimes",
    "thruk_delete_comment",
    "thruk_delete_downtime",
    "thruk_delete_downtimes_by_filter",
    "thruk_get_downtime",
    "thruk_notifications",
    "thruk_recheck",
    "thruk_remove_acknowledgement",
    "thruk_schedule_downtime",
    "thruk_schedule_host_services_downtime",
    "thruk_schedule_hostgroup_downtime",
    "thruk_schedule_propagated_host_downtime",
    "thruk_schedule_servicegroup_downtime",
]
