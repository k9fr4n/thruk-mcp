# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`thruk-mcp` is a **Model Context Protocol (MCP) server** that exposes the [Thruk](https://www.thruk.org/) monitoring REST API (Naemon / Nagios / Icinga / Shinken) as ~65 MCP tools, so an LLM can query hosts/services, run analytics, and execute write actions (downtimes, acks, rechecks) over Thruk's REST API. Python package, `src/` layout, packaged with hatchling, published to PyPI and `ghcr.io/k9fr4n/thruk-mcp`.

## Commands

Run the exact checks CI runs (see `.github/workflows/ci.yml`, gates at 80% coverage):

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest -v --cov=thruk_mcp --cov-fail-under=80
```

Dev setup: `pip install -e ".[dev]"` then `pre-commit install`.

Single test / by marker:

```bash
pytest tests/test_tools.py::test_name -v       # one test
pytest -m integration                          # live suite (skipped by default — see below)
```

`pytest.ini_options.addopts` defaults to `-m 'not integration'`, so plain `pytest` never hits a live Thruk. The integration suite needs a running instance:

```bash
docker compose -f compose.test.yml up -d       # OMD demo on :8443 (HTTPS only), wait ~30s
THRUK_BASE_URL=https://localhost:8443/demo/thruk \
  THRUK_VERIFY_SSL=false \
  THRUK_API_KEY=$(./scripts/get-test-api-key.sh) \
  pytest -m integration
```

Run the server locally: `thruk-mcp` (stdio) or `thruk-mcp --listen 8001` (Streamable-HTTP, endpoint `/mcp`). `--transport {stdio,streamable-http,sse}` picks the transport explicitly; `--listen` alone implies `streamable-http`. SSE (`--transport sse`, `/sse` + `/messages/`) is deprecated. `--stateless`/`--json-response` apply to streamable-http only (transport wiring lives in `__main__.py`). Needs `THRUK_BASE_URL` + `THRUK_API_KEY` (copy `.env.example` → `.env`).

## Architecture

**Low-level MCP SDK, not FastMCP — this is deliberate.** `server.py` uses `mcp.server.Server` and defines every tool's `inputSchema` *explicitly*. FastMCP derives schemas from type hints via `get_type_hints()`, which fails for closures and yields empty `properties: {}`; the Docker MCP Gateway then silently drops all tool arguments. Consequently: arguments arrive in `call_tool` as a **raw `dict`** (no Pydantic model), and schemas are hand-built with the `_s/_str/_int/_bool/_OPT_*` helpers in `tools/base.py`. Don't reintroduce annotation-driven schema generation.

**Tool registration is single-source-of-truth via `ToolSpec`** (`tools/base.py`): `(name, fn, schema, is_write)`. Each tool group module owns a co-located `*_REGISTRY: list[ToolSpec]`. `tools/__init__.py` splices them — in a fixed, byte-for-byte order — into the global `TOOL_REGISTRY`. `server.py` derives everything else from it:
- `_TOOL_DISPATCH = {name: fn}`
- `_TOOL_SCHEMAS = {name: schema}`
- `WRITE_TOOLS = frozenset(name for is_write)`

This is why `WRITE_TOOLS` can never fall out of sync with dispatch/schema. Adding a tool = exactly one `ToolSpec` entry in the right module's registry.

**Tool group modules** (`src/thruk_mcp/tools/`):
- `inventory.py` — read-only listing/inventory/availability (hosts, services, groups, stats, SLA).
- `history.py` — logs/alerts/notifications + trends (noisy/flap/heatmaps/recurring/reliability).
- `triage.py` — problem-intelligence analytics (oldest/unacked/stale/problem_counts/stale_checks).
- `commands.py` — read commands + the **write** tools (acks, downtimes, rechecks, comments).
- `escape.py` — `thruk_query` (any REST endpoint) and `thruk_run_background_query`.
- `perfdata.py` — performance-data tools.

**`server.py` re-exports nearly everything** from `tools.*`, `filters`, `helpers`, `constants` so legacy `from thruk_mcp.server import X` imports keep working after the package split (issues #147/#256+). When moving code, preserve these re-exports.

**Layering (avoid import cycles):** tool modules import shared infra from `helpers.py`, `filters.py`, `constants.py` — **never from `server`**. `server` sits on top.

**`client.py` — `ThrukClient`** is the single async httpx wrapper. It owns *all* retry logic (jittered exponential backoff on 429/5xx; transport-level `retries=0` on purpose — see #150 for the double-retry amplification it prevents). Key methods: `request`, `get`/`post`, `get_with_fallback`, `get_all` (pagination), `run_background` (`?background=1`). Short-TTL cache (`cache.py`) only for slow-changing `CACHEABLE_PATHS`. Pool size capped well below httpx defaults to avoid saturating one Thruk core under LLM fan-out.

**`filters.py`** translates a composable AND/OR filter tree into Thruk REST params: pure AND → bracket-operator params (`name[regex]=`, `state=`, `_VARNAME=`); any OR → a single `q=` expression. Log-family tools have no group/custom-var columns, so they resolve `hostgroup`/`custom_var` via a secondary `/hosts` lookup → `host_name[regex]` (see `helpers._resolve_log_filter`, `_resolve_hosts_to_regex_from_params`).

**Security model** (`config.ThrukConfig`, all `THRUK_*` env vars):
- `THRUK_READ_ONLY=true` strips every `WRITE_TOOLS` tool from the registry at `build_server()` time; `thruk_query` stays but is GET-only enforced in `escape.py`.
- `THRUK_ENABLED_TOOLS` — fnmatch allowlist.
- `THRUK_AUDIT_LOG` — JSON line on the `thruk_mcp.audit` logger for every write call (`audit.py`, with `scrub()` redacting secrets; `ThrukConfig.__repr__` redacts `api_key`).

## Conventions (from CONTRIBUTING.md)

- No direct push to `main` — PR only, squash-merge. Branch prefixes `feat/ fix/ chore/ docs/ refactor/ test/`; PR titles are Conventional Commits (`feat(server): ...`).
- New tool checklist: `thruk_` snake_case name; type every param (no `**kwargs`); LLM-facing concise docstring; surface `ThrukError` verbatim. **List tools must** take `limit/offset/sort/columns` via `_list_params()` + a `DEFAULT_*_COLUMNS` constant. **Write tools must** set `is_write=True` so read-only/audit apply. Add a `respx`-mocked routing test in `tests/test_tools.py` (assert method, URL path, key params), then regenerate `catalog/tools.json` with `python scripts/gen_tools_json.py` (generated from the live registry via `list_tools()` — never hand-edited; CI runs `--check`).
- New `THRUK_*` env var must appear in **three** places: `config.py` (`ThrukConfig` field + `from_env()`), `.env.example`, and `catalog/server.yaml` (`config.env` *and* `config.parameters`).
- `catalog/` follows the docker/mcp-registry schema; `tools.json` and `metadata.json` are generated from the live registry by `scripts/gen_tools_json.py` / `scripts/gen_metadata.py` (never hand-edited).
