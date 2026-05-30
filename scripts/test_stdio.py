"""Simulate the MCP stdio protocol and print what tools/list returns."""
import json
import os
import subprocess
import sys

os.environ["THRUK_BASE_URL"] = "https://wopr-thruk-00.ecritel.net/thruk"
os.environ["THRUK_API_KEY"] = "9528fcf6be874daa4084b6fd18d3dbe6172a81d1150926b8c830fd8641077d2a_1"

msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    }},
    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "thruk_get_host", "arguments": {"host": "OWL-AD-01"},
    }},
]

stdin_data = "".join(json.dumps(m) + "\n" for m in msgs).encode()

proc = subprocess.run(
    [sys.executable, "-m", "thruk_mcp"],
    input=stdin_data,
    capture_output=True,
    timeout=20,
    env={**os.environ, "PYTHONPATH": "src"},
)

for line in proc.stdout.decode().splitlines():
    if not line.strip():
        continue
    try:
        msg = json.loads(line)
        mid = msg.get("id")
        if mid == 1:
            print("PASS initialize:", msg.get("result", {}).get("serverInfo", {}))
        elif mid == 2:
            tools = msg.get("result", {}).get("tools", [])
            print(f"PASS tools/list: {len(tools)} tools")
            t = next((x for x in tools if x["name"] == "thruk_get_host"), None)
            if t:
                schema = t.get("inputSchema", {})
                print(f"  thruk_get_host.inputSchema = {json.dumps(schema)}")
                print(f"  required = {schema.get('required')}")
                print(f"  properties = {list(schema.get('properties', {}).keys())}")
        elif mid == 3:
            content = msg.get("result", {}).get("content", [])
            text = content[0].get("text", "") if content else ""
            is_error = msg.get("result", {}).get("isError", False)
            status = "FAIL (isError)" if is_error else "PASS"
            print(f"{status} tools/call thruk_get_host(OWL-AD-01):")
            print(" ", text[:300])
    except json.JSONDecodeError:
        print(f"  [non-JSON]: {line[:100]}")

if proc.returncode not in (0, None):
    print(f"\nProcess exited with code {proc.returncode}")
if proc.stderr:
    print("STDERR:", proc.stderr.decode()[:500])
