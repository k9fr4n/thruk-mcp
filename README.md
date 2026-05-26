# thruk-mcp

[![CI](https://github.com/k9fr4n/thruk-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/k9fr4n/thruk-mcp/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/k9fr4n/thruk-mcp/branch/main/graph/badge.svg)](https://codecov.io/gh/k9fr4n/thruk-mcp)
[![PyPI](https://img.shields.io/pypi/v/thruk-mcp)](https://pypi.org/project/thruk-mcp/)
[![PyPI downloads](https://img.shields.io/pypi/dm/thruk-mcp)](https://pypi.org/project/thruk-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)](https://pypi.org/project/thruk-mcp/)
[![ghcr.io](https://img.shields.io/badge/ghcr.io-k9fr4n%2Fthruk--mcp-blue)](https://github.com/k9fr4n/thruk-mcp/pkgs/container/thruk-mcp)
[![GitHub release](https://img.shields.io/github/v/release/k9fr4n/thruk-mcp)](https://github.com/k9fr4n/thruk-mcp/releases)

**Model Context Protocol (MCP) server for [Thruk](https://www.thruk.org/)** — the unified web frontend for [Naemon](https://naemon.io), Nagios, [Icinga](https://icinga.com/) and [Shinken](http://www.shinken-monitoring.org/).

Expose Thruk's REST API to MCP-compatible clients (Claude Desktop, Dust, LibreChat, OpenWebUI...) so that an LLM can query hosts/services, schedule downtimes, acknowledge problems, force rechecks and more in natural language.

## Features

- **Read**: hosts, services, hostgroups, servicegroups, downtimes, comments, sites, aggregated stats, current problems
- **Write**: schedule/delete downtimes, acknowledge & remove acks, force rechecks
- **Escape hatch**: `thruk_query` tool to call *any* Thruk REST endpoint
- **Multi-backend** support (Thruk federated sites): pass `backends="prod,dr"` to any tool
- **Two transports**: stdio (default) or Streamable-HTTP (`--listen <port>`)
- **Async httpx client** with proper error handling and TLS verification
- Tested with `pytest` + `respx`, linted with `ruff`, packaged with `hatchling`

## Quick start

### 1. Configure

```bash
cp .env.example .env
$EDITOR .env   # set THRUK_BASE_URL and THRUK_API_KEY
```

An API key can be created from the Thruk **user profile page** (requires `api_keys_enabled` in `thruk_local.conf`) or via the REST API itself.

### 2a. Run with Docker

```bash
docker compose up -d
# MCP Streamable-HTTP endpoint: http://localhost:8001/mcp
```

### 2b. Run locally

```bash
pip install thruk-mcp        # or: pipx install thruk-mcp

# stdio mode (for Claude Desktop, LibreChat, etc.)
thruk-mcp

# HTTP mode
thruk-mcp --listen 8001
```

> For local development of the project itself, see [CONTRIBUTING.md](CONTRIBUTING.md).

### 3. Wire it to an MCP client

**Claude Desktop** (`~/.config/Claude/claude_desktop_config.json` or macOS equivalent):

```json
{
  "mcpServers": {
    "thruk": {
      "command": "thruk-mcp",
      "env": {
        "THRUK_BASE_URL": "https://monitor.example.com/thruk",
        "THRUK_API_KEY": "xxxxxxxx"
      }
    }
  }
}
```

### 4. Use with the [Docker MCP Gateway](https://github.com/docker/mcp-gateway)

The image at `ghcr.io/k9fr4n/thruk-mcp:latest` defaults to **stdio** transport, so it can be spawned natively by the gateway.

#### Option A — Private local catalog

```bash
# 1. Create your private catalog
docker mcp catalog create thruk-private

# 2. Register this server (catalog/server.yaml ships with the repo)
docker mcp catalog add thruk-private thruk-mcp ./catalog/server.yaml

# 3. Configure credentials & enable
docker mcp secret set thruk-mcp.api_key=YOUR_KEY
docker mcp config write thruk-mcp.base_url=https://monitor.example.com/thruk
docker mcp server enable thruk-mcp

# 4. Run the gateway with your catalog
docker mcp gateway run --catalog thruk-private
```

Then point any MCP client (Claude Desktop, VS Code, Cursor, ...) at the gateway as documented [here](https://www.docker.com/blog/build-custom-mcp-catalog/).

#### Option B — Submit upstream

`catalog/server.yaml`, `catalog/tools.json` and `catalog/readme.md` follow the [docker/mcp-registry](https://github.com/docker/mcp-registry) schema and can be submitted to the official Docker MCP Catalog via PR.

## What's exposed

### 49 MCP Tools

**Read — state**
`thruk_list_hosts`, `thruk_get_host`, `thruk_list_services`, `thruk_get_service`,
`thruk_list_hostgroups`, `thruk_list_servicegroups`, `thruk_problems`, `thruk_stats`,
`thruk_sites`.

**Read — history & comments**
`thruk_list_logs`, `thruk_list_alerts`, `thruk_list_notifications`, `thruk_recent_events`,
`thruk_list_comments`, `thruk_list_downtimes`, `thruk_get_downtime`.

**Read — noise & flap analysis**
`thruk_top_noisy_hosts` (hosts ranked by alert count over a window),
`thruk_top_noisy_services` (services ranked by alert count),
`thruk_flap_summary` (hosts/services ranked by state transition count).

**Read — problem intelligence**
`thruk_oldest_problems` (unhandled problems sorted by age, oldest first),
`thruk_unacked_critical` (CRITICAL/DOWN not acknowledged for > N minutes),
`thruk_stale_acks` (acknowledgements older than N days — forgotten problems),
`thruk_problems_by_hostgroup` (problem counts aggregated per hostgroup).

**Read — analytics**
`thruk_alert_heatmap` (alert counts bucketed by time, useful for spotting recurring
patterns), `thruk_concurrent_failures` (windows where multiple hosts failed simultaneously),
`thruk_recurring_problems` (hosts/services generating repeated alerts over a window).

**Read — availability / SLA**
`thruk_host_availability` (uptime % for a single host — `time_up_percent`, `time_down_percent`,
`time_unreachable_percent` and scheduled equivalents),
`thruk_service_availability` (ok/warning/critical/unknown % for a single service),
`thruk_hostgroup_availability` (availability for all hosts or services in a hostgroup,
sorted worst-first; `type` = `hosts` | `services` | `both`).
All three accept `since`/`until` (Thruk relative or ISO) or a `timeperiod` shortcut
(`lastmonth`, `thismonth`, `last24hours`, `lastweek`, …).

**Write — downtime management**
`thruk_schedule_downtime` (host/service), `thruk_schedule_host_services_downtime`
(all services of a host), `thruk_schedule_propagated_host_downtime` (parent+children),
`thruk_schedule_hostgroup_downtime`, `thruk_schedule_servicegroup_downtime`,
`thruk_delete_downtime`, `thruk_delete_active_downtimes`,
`thruk_delete_downtimes_by_filter`.

**Write — problem handling**
`thruk_acknowledge`, `thruk_remove_acknowledgement`, `thruk_recheck`,
`thruk_notifications` (enable/disable host or service notifications, with optional
cascade to all services of a host).

**Escape hatches**
`thruk_query` (raw call to any REST endpoint), `thruk_run_background_query`
(long-running endpoint via Thruk's `?background=1` mechanism with automatic
job polling).

> All list-style tools share a consistent `limit` / `offset` / `sort` / `columns`
> contract. By default they return a tight subset of columns (~10 fields per row)
> to keep LLM token consumption low. Pass `columns=""` to opt out and receive
> every column the Thruk row contains.

### 5 MCP Resources

URI templates that MCP clients with a resource browser (Claude Desktop, VS
Code, ...) can "open" like files:

| URI | Content |
| --- | --- |
| `thruk://hosts/{name}` | Full host JSON |
| `thruk://services/{host}/{service}` | Full service JSON |
| `thruk://hostgroups/{name}` | Host group config + members |
| `thruk://problems` | Current unhandled problems (hosts + services) |
| `thruk://stats` | Aggregated host/service stats (cached) |

### 3 MCP Prompts

Pre-canned workflows the user can invoke as a slash-command in the MCP
client UI:

| Prompt | Arguments | Purpose |
| --- | --- | --- |
| `investigate_alert` | `host`, optional `service` | 7-step incident triage |
| `schedule_maintenance` | `target`, `duration_minutes`, `kind` | Safe downtime workflow with confirmation |
| `diagnose_flapping` | `host`, `service` | Root-cause a flapping service (uses `thruk_flap_summary`) |

## Robustness

- **Connection retries** — `httpx.AsyncHTTPTransport(retries=3)` handles DNS
  failures, connection refusals, TLS handshakes.
- **HTTP retries with backoff** — 5xx and 429 responses are retried up to
  3 times with exponential backoff + jitter (cap 5 s).
- **Opt-in TTL cache** — slow-moving endpoints (`/sites`, `/processinfo`,
  `/hosts/stats`, `/services/stats`, `/contacts`, `/timeperiods`, ...) are
  cached in-process for 15 s. Any tool can request caching via
  `cache_ttl=` on the underlying client. This absorbs the burst of identical
  calls an LLM agent typically issues across a multi-tool turn.
- **Pagination helper** — `ThrukClient.get_all()` is an async generator that
  iterates pages of 500 rows up to a configurable hard limit (default 50 000),
  so internal callers can scan entire backends without manual offset math.
- **Long-running queries** — the `thruk_run_background_query` tool wraps
  Thruk's `?background=1` flow and polls `/thruk/jobs/<id>/output` until the
  job completes (5 min default timeout).

## Environment variables

### Connection

| Variable                  | Default                  | Description                                              |
| ------------------------- | ------------------------ | -------------------------------------------------------- |
| `THRUK_BASE_URL`          | `http://localhost/thruk` | Thruk URL (no trailing slash)                            |
| `THRUK_API_KEY`           | *(required)*             | `X-Thruk-Auth-Key` header                                |
| `THRUK_AUTH_USER`         |                          | Impersonation user (superuser key only)                  |
| `THRUK_VERIFY_SSL`        | `true`                   | Set `false` for self-signed certs                        |
| `THRUK_TIMEOUT`           | `30`                     | HTTP timeout in seconds                                  |
| `THRUK_DEFAULT_BACKENDS`  |                          | CSV of default backend names (federated Thruk)           |

### Security / multi-tenant (v0.6)

| Variable                  | Default | Description                                                           |
| ------------------------- | ------- | --------------------------------------------------------------------- |
| `THRUK_READ_ONLY`         | `false` | Strip every write tool (ack, downtime, recheck, ...)                  |
| `THRUK_ENABLED_TOOLS`     |         | Allowlist of tool names. CSV with fnmatch wildcards. Empty = all      |
| `THRUK_AUDIT_LOG`         | `true`  | Emit one JSON audit line on stderr per write tool invocation          |
| `THRUK_MAX_CONCURRENT`    | `0`     | Cap of concurrent in-flight HTTP requests. 0 = unlimited              |

## Security

- **Read-only mode** — set `THRUK_READ_ONLY=true` to remove every write tool
  (`thruk_acknowledge`, `thruk_schedule_*_downtime`, `thruk_recheck`,
  `thruk_delete_*`, `thruk_run_background_query`) from the MCP server. The
  LLM literally cannot mutate monitoring state. Use this for general-purpose
  agents that should only observe.
- **Tool allowlist** — `THRUK_ENABLED_TOOLS=thruk_list_*,thruk_problems,thruk_stats`
  restricts the exposed surface to the listed tools (fnmatch wildcards
  supported). Useful when fronting multiple LLM clients with the same gateway
  but different scopes.
- **Audit log** — every write tool invocation emits one JSON line on
  `thruk_mcp.audit` (stderr by default):

  ```json
  {"ts":"2026-05-17T22:00:00+00:00","tool":"thruk_acknowledge","user":"alice",
   "args":{"host":"srv01","comment":"investigating"},"target":"srv01","status":"ok"}
  ```

  Disable with `THRUK_AUDIT_LOG=false`. Sensitive keys (`api_key`, `password`,
  `token`) are redacted as `***` before logging.
- **Rate limit** — `THRUK_MAX_CONCURRENT=8` caps in-flight HTTP requests with
  an `asyncio.Semaphore`. Combined with the v0.3 TTL cache, this protects the
  Thruk core from an LLM that loops on tools or chains them aggressively.

## Development

```bash
pip install -e ".[dev]"
pre-commit install                              # one-time setup of git hooks

ruff check src tests && ruff format src tests   # lint + format
mypy src                                        # type-check
pytest -v --cov=thruk_mcp --cov-fail-under=80   # tests with coverage gate
```

Conventions:

- Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
  `test:`).
- No direct push to `main`: branch → PR → squash merge.
- Any new tool must come with a `respx`-mocked unit test in `tests/test_tools.py`
  and an entry in `catalog/tools.json` (Docker MCP Registry contract).
- CI gate: `ruff`, `ruff format --check`, `mypy`, `pytest` with **80 %
  coverage minimum**.

## References

- Thruk REST API: <https://www.thruk.org/documentation/rest.html>
- Thruk REST commands: <https://www.thruk.org/documentation/rest_commands.html>
- MCP spec: <https://spec.modelcontextprotocol.io/>
- Inspired by: <https://github.com/lausser/omd-mcp> (initial proof-of-concept)

## Project docs

- [CHANGELOG.md](CHANGELOG.md) — what changed in each release.
- [UPGRADING.md](UPGRADING.md) — per-version migration notes.
- [SUPPORT.md](SUPPORT.md) — supported Python / Thruk / MCP-client versions,
  security policy, release cadence.
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, PR conventions, tool /
  env-var contribution checklists.

## License

MIT — see [LICENSE](LICENSE).
