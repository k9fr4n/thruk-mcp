"""Tests for issue #254: ``thruk_problems`` ignored ``host_custom_var`` on /hosts.

Before the fix:
    ``compile_filter_problems`` routed a ``host_custom_var`` leaf to the
    services sub-query only (``_HOST{VAR}``). The ``/hosts`` call was sent
    with NO custom-var constraint, so every current host problem leaked
    through regardless of the requested variable. A bogus value returned a
    non-empty ``hosts`` list instead of ``[]``.

After the fix:
    The ``host_custom_var`` branch mirrors ``custom_var``: ``_{VAR}`` on
    ``/hosts`` and ``_HOST{VAR}`` on ``/services``. A bogus value yields
    ``{"hosts": [], "services": []}``.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import ok


@pytest.mark.asyncio
async def test_problems_host_custom_var_constrains_hosts_query(mocked_server) -> None:
    """host_custom_var leaf -> _KERNEL on /hosts AND _HOSTKERNEL on /services."""
    mcp, router = mocked_server
    r_h = router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    r_s = router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    await mcp.call_tool(
        "thruk_problems",
        {
            "filter": {
                "type": "leaf",
                "field": "host_custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "windows"},
            }
        },
    )
    hp = r_h.calls.last.request.url.params
    sp = r_s.calls.last.request.url.params
    # Regression guard for issue #254: the /hosts sub-query must carry the
    # host-level custom-var constraint, otherwise it leaks every host problem.
    assert hp["_KERNEL"] == "windows"
    assert sp["_HOSTKERNEL"] == "windows"
    # /hosts uses _{VAR}, not _HOST{VAR}.
    assert "_HOSTKERNEL" not in hp


@pytest.mark.asyncio
async def test_problems_bogus_host_custom_var_returns_empty(mocked_server) -> None:
    """A non-matching host_custom_var value yields {"hosts": [], "services": []}.

    With the bug the /hosts query was unconstrained and returned host
    problems; here the filtered endpoints return nothing and both lists
    must be empty.
    """
    mcp, router = mocked_server
    router.get("https://thruk.test/r/hosts").mock(return_value=ok([]))
    router.get("https://thruk.test/r/services").mock(return_value=ok([]))
    result = await mcp.call_tool(
        "thruk_problems",
        {
            "filter": {
                "type": "leaf",
                "field": "host_custom_var",
                "op": "eq",
                "value": {"var": "KERNEL", "val": "bogusvalue_zzz"},
            }
        },
    )
    payload = json.loads(result[0].text)
    assert payload == {"hosts": [], "services": []}
