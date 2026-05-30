"""Regression tests for issue #260 (server.py split: tools/history.py).

The nine logs/history/trends tools and their private helpers moved from
``thruk_mcp.server`` into ``thruk_mcp.tools.history``. These tests pin the
refactor invariants:

* the tools/helpers are importable from the new module;
* ``server`` still re-exports the exact same objects (backward compat);
* the co-located registries splice into ``TOOL_REGISTRY`` preserving order;
* ``history.py`` never imports ``server`` (no cycle);
* one moved tool still behaves identically end-to-end (respx-mocked).
"""

from __future__ import annotations

import inspect
import json
from urllib.parse import parse_qs

import pytest

from tests.conftest import ok
from thruk_mcp import server
from thruk_mcp.tools import history

_HISTORY_TOOL_NAMES = [
    "thruk_top_noisy_hosts",
    "thruk_top_noisy_services",
    "thruk_flap_summary",
    "thruk_alert_heatmap",
    "thruk_recurring_problems",
    "thruk_list_logs",
    "thruk_list_alerts",
    "thruk_list_notifications",
    "thruk_notification_summary",
    "thruk_recent_events",
]

_HISTORY_HELPERS = [
    "_resolve_hosts_to_regex",
    "_fetch_logs",
    "_aggregate_alerts",
    "_coerce_hours_to_since",
]


def _post_params(call) -> dict[str, str]:
    body = call.request.content.decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


@pytest.mark.parametrize("name", _HISTORY_TOOL_NAMES + _HISTORY_HELPERS)
def test_symbol_lives_in_history_module(name: str) -> None:
    obj = getattr(history, name)
    assert obj is not None
    assert obj.__module__ == "thruk_mcp.tools.history"


@pytest.mark.parametrize("name", _HISTORY_TOOL_NAMES + _HISTORY_HELPERS)
def test_server_reexports_same_object(name: str) -> None:
    # Backward compat: ``from thruk_mcp.server import <name>`` keeps working
    # and resolves to the *same* object now defined in tools/history.py.
    assert getattr(server, name) is getattr(history, name)


def test_history_registry_is_trends_plus_logs() -> None:
    expected = [*history.HISTORY_TRENDS_REGISTRY, *history.HISTORY_LOGS_REGISTRY]
    assert expected == history.HISTORY_REGISTRY
    names = [spec.name for spec in history.HISTORY_REGISTRY]
    assert names == _HISTORY_TOOL_NAMES


def test_history_specs_spliced_into_tool_registry() -> None:
    registry_names = [spec.name for spec in server.TOOL_REGISTRY]
    # All nine present exactly once.
    for name in _HISTORY_TOOL_NAMES:
        assert registry_names.count(name) == 1
    # Trends block keeps its internal order and leads the registry.
    trends = [s.name for s in history.HISTORY_TRENDS_REGISTRY]
    assert registry_names[: len(trends)] == trends
    # Logs block keeps its internal relative order within the full registry.
    log_positions = [
        registry_names.index(n) for n in [s.name for s in history.HISTORY_LOGS_REGISTRY]
    ]
    assert log_positions == sorted(log_positions)
    # The logs block registers after the inventory/get_downtime tools.
    assert registry_names.index("thruk_list_logs") > registry_names.index("thruk_get_downtime")


def test_history_module_does_not_import_server() -> None:
    src = inspect.getsource(history)
    assert "import server" not in src
    assert "from ..server" not in src
    assert "from thruk_mcp.server" not in src


@pytest.mark.asyncio
async def test_moved_list_alerts_still_works_end_to_end(mocked_server) -> None:
    # Behavioural equivalence: the moved thruk_list_alerts must still POST to
    # /logs with the disambiguating type[~] + class=1 guards (issues #176/#198).
    proxy, router = mocked_server
    route = router.post("https://thruk.test/r/logs").mock(
        return_value=ok([{"time": 1700000000, "type": "SERVICE ALERT", "state": 2}])
    )
    result = await proxy.call_tool("thruk_list_alerts", {"since": "-1h"})
    payload = json.loads(result[0].text)
    assert isinstance(payload, list)
    assert route.called
    params = _post_params(route.calls.last)
    assert params["class"] == "1"
    assert params["type[~]"] == "^(HOST|SERVICE) ALERT"
