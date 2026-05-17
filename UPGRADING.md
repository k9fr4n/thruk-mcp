# Upgrade Guide

Notes on migrating between MINOR / MAJOR releases. Patch releases never
require migration steps.

## Upgrading to 1.0.0 (from 0.5.x)

Nothing breaks. v1.0.0 is a quality / docs milestone (support policy,
contributor guide, integration test scaffolding, PyPI publish workflow).
The API surface is identical to 0.5.0.

The `1.0` mark commits the project to semver: any future MAJOR bump will
be announced and documented here ahead of release.

## Upgrading to 0.5.0 (from 0.4.x)

### New env vars (all optional, safe defaults)

| Var | Default | What |
| --- | --- | --- |
| `THRUK_READ_ONLY` | `false` | Strip every write tool |
| `THRUK_ENABLED_TOOLS` |  | fnmatch allowlist |
| `THRUK_AUDIT_LOG` | `true` | One JSON line on stderr per write call |
| `THRUK_MAX_CONCURRENT` | `0` | Cap of in-flight HTTP requests |

If you previously parsed stderr expecting nothing structured, you may now see
JSON lines from the `thruk_mcp.audit` logger — set `THRUK_AUDIT_LOG=false`
to restore the old behaviour.

## Upgrading to 0.4.0 (from 0.3.x)

**Breaking** — small API cleanup on the two escape-hatch tools:

```python
# Before (0.3.x)
thruk_query(path="/hosts", params_json='{"limit":5}')
thruk_run_background_query(path="/...", data_json='{"x":1}')

# After (0.4.0+)
thruk_query(path="/hosts", params={"limit": 5})
thruk_run_background_query(path="/...", data={"x": 1})
```

FastMCP’s pydantic validation auto-decoded JSON-looking strings before they
reached the function, which made the old JSON-string API impossible to call
in practice. Dicts are the natural shape since MCP RPC already carries JSON.

## Upgrading to 0.3.0 (from 0.2.x)

No breaking changes. New MCP **resources** (`thruk://hosts/{name}`, ...) and
**prompts** (`investigate_alert`, `schedule_maintenance`,
`diagnose_flapping`) appear automatically in compatible clients. Retry +
cache + paginator are transparent.

## Upgrading to 0.2.0 (from 0.1.x)

**Breaking** — every list tool now returns a tight subset of columns by
default (~10 fields instead of ~80) to drastically cut LLM token use. Pass
`columns=""` to opt out and get every column the Thruk row contains, matching
the 0.1 behaviour.

**Bug fix** — the `thruk_acknowledge` payload keys are now correct
(`sticky_ack`, `send_notification`, `persistent_comment`). Prior to 0.2.0
these were sent as `sticky`, `notify`, `persistent` and silently ignored by
the Thruk core, meaning `notify=False` did not actually suppress
notifications. Re-check any automation that depended on the broken
behaviour.
