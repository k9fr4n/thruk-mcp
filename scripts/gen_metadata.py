#!/usr/bin/env python3
"""Generate catalog/metadata.json for the Docker MCP Gateway io.docker.server.metadata label."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("THRUK_BASE_URL", "x")
os.environ.setdefault("THRUK_API_KEY", "x")

from thruk_mcp.server import build_server


def schema_to_arguments(input_schema: dict) -> list:
    props = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    args = []
    for name, prop in props.items():
        ptype = prop.get("type", "string")
        if "anyOf" in prop:
            non_null = [x.get("type") for x in prop["anyOf"] if x.get("type") != "null"]
            ptype = non_null[0] if non_null else "string"
        type_map = {"integer": "integer", "number": "number",
                    "boolean": "boolean", "array": "array", "object": "object"}
        arg = {
            "name": name,
            "type": type_map.get(ptype, "string"),
            "desc": prop.get("description", prop.get("title", "")),
        }
        if name not in required:
            arg["optional"] = True
        args.append(arg)
    return args


def main():
    server = build_server()
    tools_raw = server._tool_manager.list_tools()
    tools = []
    for t in tools_raw:
        args = schema_to_arguments(t.parameters)
        tool = {"name": t.name, "description": " ".join(t.description.split())}
        if args:
            tool["arguments"] = args
        tools.append(tool)

    metadata = {
        "name": "thruk-mcp",
        "type": "server",
        "title": "Thruk MCP Server",
        "description": (
            "MCP server exposing the Thruk REST API "
            "(Naemon/Nagios/Icinga monitoring) to MCP-compatible LLM clients."
        ),
        "secrets": [{"name": "thruk-mcp.api_key", "env": "THRUK_API_KEY"}],
        "env": [
            {"name": "THRUK_BASE_URL", "value": "{{thruk-mcp.base_url}}"},
            {"name": "THRUK_AUTH_USER", "value": "{{thruk-mcp.auth_user}}"},
            {"name": "THRUK_VERIFY_SSL", "value": "{{thruk-mcp.verify_ssl}}"},
            {"name": "THRUK_READ_ONLY", "value": "{{thruk-mcp.read_only}}"},
        ],
        "tools": tools,
    }

    out = os.path.join(os.path.dirname(__file__), "..", "catalog", "metadata.json")
    with open(out, "w") as fh:
        json.dump(metadata, fh, separators=(",", ":"))
        fh.write("\n")
    print(f"Written {len(tools)} tools to catalog/metadata.json", file=sys.stderr)
    print(json.dumps(metadata, separators=(",", ":")))


if __name__ == "__main__":
    main()
