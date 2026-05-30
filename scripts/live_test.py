"""Live integration test — tools/list schema + tool calls via stdio MCP protocol."""
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("THRUK_BASE_URL", "https://wopr-thruk-00.ecritel.net/thruk")
os.environ.setdefault(
    "THRUK_API_KEY",
    "9528fcf6be874daa4084b6fd18d3dbe6172a81d1150926b8c830fd8641077d2a_1",
)

from thruk_mcp.client import ThrukClient, ThrukError
from thruk_mcp.config import ThrukConfig

results: list[tuple[str, bool, str]] = []


async def t(label: str, coro):
    try:
        r = await coro
        out = json.dumps(r, indent=2, default=str)
        print(f"PASS {label}")
        print(out[:800])
        print()
        results.append((label, True, ""))
    except ThrukError as e:
        print(f"FAIL {label} -- ThrukError: {e}")
        results.append((label, False, str(e)))
    except Exception as e:
        print(f"FAIL {label} -- {type(e).__name__}: {e}")
        results.append((label, False, str(e)))


async def main():
    cfg = ThrukConfig.from_env()
    async with ThrukClient(cfg) as c:
        host = "OWL-AD-01"

        await t(
            "thruk_get_host(" + host + ")",
            c.get("/hosts/" + host),
        )

        await t(
            "thruk_list_services(host=" + host + ")",
            c.get("/services", params={
                "host_name": host,
                "columns": "description,state,plugin_output",
                "limit": "20",
            }),
        )

        await t("thruk_stats hosts", c.get("/hosts/stats"))
        await t("thruk_stats services", c.get("/services/stats"))

        await t(
            "thruk_problems unacked",
            c.get("/hosts", params={
                "state[gte]": "1",
                "acknowledged": "0",
                "scheduled_downtime_depth": "0",
                "columns": "name,state,plugin_output,last_state_change",
                "limit": "10",
            }),
        )

    print("=" * 60)
    ok = sum(1 for _, b, _ in results if b)
    print("Results: " + str(ok) + "/" + str(len(results)) + " passed")
    for name, b, err in results:
        if not b:
            print("  FAIL " + name + ": " + err)


def test_stdio_protocol():
    """Simulate MCP client: send initialize + tools/list + tools/call via stdio."""
    print("\n" + "=" * 60)
    print("STDIO PROTOCOL TEST")
    print("=" * 60)

    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "live-test", "version": "0"},
        }},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "thruk_get_host",
            "arguments": {"host": "OWL-AD-01"},
        }},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "thruk_stats",
            "arguments": {},
        }},
    ]

    venv_python = os.path.join(os.path.dirname(__file__), "..", ".venv", "bin", "thruk-mcp")
    stdin_data = "".join(json.dumps(m) + "\n" for m in msgs).encode()

    try:
        proc = subprocess.run(
            [venv_python],
            input=stdin_data,
            capture_output=True,
            timeout=20,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        print("FAIL stdio test timed out")
        return

    lines = proc.stdout.decode().splitlines()
    for line in lines:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            mid = msg.get("id")
            if mid == 1:
                print("PASS initialize:", msg.get("result", {}).get("serverInfo", {}))
            elif mid == 2:
                tools = msg.get("result", {}).get("tools", [])
                print(f"PASS tools/list: {len(tools)} tools registered")
                # Check schemas for key tools
                for tool in tools:
                    if tool["name"] in ("thruk_get_host", "thruk_list_hosts"):
                        schema = tool.get("inputSchema", {})
                        props = schema.get("properties", {})
                        required = schema.get("required", [])
                        print(
                            f"  {tool['name']}: properties="
                            f"{list(props.keys())}, required={required}"
                        )
            elif mid == 3:
                content = msg.get("result", {}).get("content", [])
                text = content[0].get("text", "") if content else ""
                is_error = msg.get("result", {}).get("isError", False)
                status = "FAIL (isError)" if is_error else "PASS"
                print(f"{status} tools/call thruk_get_host(OWL-AD-01):")
                print(text[:400])
            elif mid == 4:
                content = msg.get("result", {}).get("content", [])
                text = content[0].get("text", "") if content else ""
                is_error = msg.get("result", {}).get("isError", False)
                status = "FAIL (isError)" if is_error else "PASS"
                print(f"{status} tools/call thruk_stats:")
                print(text[:300])
        except json.JSONDecodeError:
            print(f"  [non-JSON line]: {line[:100]}")

    if proc.returncode not in (0, None):
        print(f"Process exited with code {proc.returncode}")
    if proc.stderr:
        print("STDERR:", proc.stderr.decode()[:500])


asyncio.run(main())
test_stdio_protocol()
