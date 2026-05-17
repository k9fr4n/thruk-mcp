from __future__ import annotations

import httpx
import pytest
import respx

from thruk_mcp.client import ThrukClient
from thruk_mcp.config import ThrukConfig

CFG = ThrukConfig(base_url="https://thruk.test", api_key="k")


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
        # 2) Polling: two 302 "still running", then 200 with the final payload
        router.get("https://thruk.test/thruk/jobs/abc/output").mock(
            side_effect=[
                httpx.Response(302, headers={"Location": "/thruk/jobs/abc/output"}),
                httpx.Response(302, headers={"Location": "/thruk/jobs/abc/output"}),
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
