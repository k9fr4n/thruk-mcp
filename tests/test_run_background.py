from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig

CFG = ThrukConfig(base_url="https://thruk.test", api_key="secret-key")

# The poll URL must go through the REST prefix so the API key is honoured.
POLL_URL = "https://thruk.test/r/thruk/jobs/abc/output"


@pytest.mark.asyncio
async def test_run_background_polls_until_200() -> None:
    async with respx.mock() as router:
        # 1) Initial POST kicks off the job
        router.post("https://thruk.test/r/hosts/srv01/cmd/schedule_host_check").mock(
            return_value=httpx.Response(
                200,
                json={"job_id": "abc", "result_url": "/thruk/jobs/abc/output"},
            )
        )
        # 2) Polling: two 302 "still running", then 200 with the final payload.
        #    URL must be routed via /r/ (REST prefix) so the API key applies.
        router.get(POLL_URL).mock(
            side_effect=[
                httpx.Response(302, headers={"Location": POLL_URL}),
                httpx.Response(302, headers={"Location": POLL_URL}),
                httpx.Response(200, json={"rc": 0, "output": "done"}),
            ]
        )
        async with ThrukClient(CFG, max_retries=0) as client:
            data = await client.run_background(
                "/hosts/srv01/cmd/schedule_host_check",
                poll_interval=0.0,
            )
        assert data == {"rc": 0, "output": "done"}


@pytest.mark.asyncio
async def test_run_background_poll_url_uses_rest_prefix() -> None:
    """Poll requests must hit /r/thruk/jobs/<id>/output, not /thruk/jobs/<id>/output.

    The /thruk/r/ REST router validates X-Thruk-Auth-Key; the bare
    /thruk/jobs/ UI path does not, causing HTTP 401.
    """
    async with respx.mock() as router:
        router.post("https://thruk.test/r/config/check").mock(
            return_value=httpx.Response(
                200,
                json={"job_id": "xyz", "result_url": "/thruk/jobs/xyz/output"},
            )
        )
        poll_route = router.get(POLL_URL.replace("abc", "xyz")).mock(
            return_value=httpx.Response(200, json={"rc": 0, "output": "ok"})
        )
        async with ThrukClient(CFG, max_retries=0) as client:
            await client.run_background("/config/check", poll_interval=0.0)

        assert poll_route.called, "Poll request was never sent to the REST-prefixed URL"
        poll_req: httpx.Request = poll_route.calls[0].request
        assert "X-Thruk-Auth-Key" in poll_req.headers, (
            "Auth header missing from poll request — API key would be rejected"
        )
        assert poll_req.headers["X-Thruk-Auth-Key"] == "secret-key"
        assert "/r/thruk/jobs/" in str(poll_req.url), (
            f"Poll URL {poll_req.url!r} does not go through the REST prefix /r/"
        )


@pytest.mark.asyncio
async def test_run_background_uses_running_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_background() must not call asyncio.get_event_loop() (deprecated in 3.10+,
    RuntimeError in 3.14). get_running_loop() must be used instead."""

    def fail_get_event_loop() -> object:
        raise AssertionError("asyncio.get_event_loop() must not be called — use get_running_loop()")

    monkeypatch.setattr(asyncio, "get_event_loop", fail_get_event_loop)

    async with respx.mock() as router:
        router.post("https://thruk.test/r/config/check").mock(
            return_value=httpx.Response(
                200,
                json={"job_id": "xyz", "result_url": "/thruk/jobs/xyz/output"},
            )
        )
        router.get(POLL_URL.replace("abc", "xyz")).mock(
            side_effect=[
                httpx.Response(302, headers={"Location": POLL_URL.replace("abc", "xyz")}),
                httpx.Response(200, json={"rc": 0, "output": "ok"}),
            ]
        )
        async with ThrukClient(CFG, max_retries=0) as client:
            data = await client.run_background("/config/check", poll_interval=0.0)

    assert data == {"rc": 0, "output": "ok"}


@pytest.mark.asyncio
async def test_run_background_pass_through_when_no_job_id() -> None:
    """If the endpoint does not support background mode, the immediate response
    should be returned verbatim."""
    async with respx.mock() as router:
        router.post("https://thruk.test/r/system/cmd/whatever").mock(
            return_value=httpx.Response(200, json={"rc": 0, "message": "ok"})
        )
        async with ThrukClient(CFG) as client:
            data = await client.run_background("/system/cmd/whatever")
        assert data == {"rc": 0, "message": "ok"}
