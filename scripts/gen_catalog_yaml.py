#!/usr/bin/env python3
"""Generate the tools: block for kdust-custom.yaml from catalog/metadata.json."""
import json
import os

metadata_path = os.path.join(os.path.dirname(__file__), "..", "catalog", "metadata.json")
with open(metadata_path) as f:
    data = json.load(f)

print("    tools:")
for tool in data["tools"]:
    print(f"      - name: {tool['name']}")
    args = tool.get("arguments")
    if args:
        print("        arguments:")
        for a in args:
            optional = ", optional: true" if a.get("optional") else ""
            desc = a.get("desc", "")
            print(
                f"          - {{name: {a['name']}, type: {a['type']}, "
                f'desc: "{desc}"{optional}}}'
            )
