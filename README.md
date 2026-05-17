# thruk-mcp

[![CI](https://github.com/k9fr4n/thruk-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/k9fr4n/thruk-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# stdio mode (for Claude Desktop, LibreChat, etc.)
thruk-mcp

# HTTP mode
thruk-mcp --listen 8001
```

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

### 29 MCP Tools

**Read — state**
`thruk_list_hosts`, `thruk_get_host`, `thruk_list_services`, `thruk_get_service`,
`thruk_list_hostgroups`, `thruk_list_servicegroups`, `thruk_problems`, `thruk_stats`,
`thruk_sites`.

**Read — history & comments**
`thruk_list_logs`, `thruk_list_alerts`, `thruk_list_notifications`, `thruk_recent_events`,
`thruk_list_comments`, `thruk_list_downtimes`, `thruk_get_downtime`.

**Write — downtime management**
`thruk_schedule_downtime` (host/service), `thruk_schedule_host_services_downtime`
(all services of a host), `thruk_schedule_propagated_host_downtime` (parent+children),
`thruk_schedule_hostgroup_downtime`, `thruk_schedule_servicegroup_downtime`,
`thruk_delete_downtime`, `thruk_delete_active_downtimes`,
`thruk_delete_downtimes_by_filter`.

**Write — problem handling**
`thruk_acknowledge`, `thruk_remove_acknowledgement`, `thruk_recheck`.

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
| `diagnose_flapping` | `host`, `service` | Root-cause a flapping service |

## Robustness

- **Connection retries** \u2014 `httpx.AsyncHTTPTransport(retries=3)` handles DNS
  failures, connection refusals, TLS handshakes.
- **HTTP retries with backoff** \u2014 5xx and 429 responses are retried up to
  3 times with exponential backoff + jitter (cap 5 s).
- **Opt-in TTL cache** \u2014 slow-moving endpoints (`/sites`, `/processinfo`,
  `/hosts/stats`, `/services/stats`, `/contacts`, `/timeperiods`, ...) are
  cached in-process for 15 s. Any tool can request caching via
  `cache_ttl=` on the underlying client. This absorbs the burst of identical
  calls an LLM agent typically issues across a multi-tool turn.
- **Pagination helper** \u2014 `ThrukClient.get_all()` is an async generator that
  iterates pages of 500 rows up to a configurable hard limit (default 50 000),
  so internal callers can scan entire backends without manual offset math.
- **Long-running queries** \u2014 the `thruk_run_background_query` tool wraps
  Thruk's `?background=1` flow and polls `/thruk/jobs/<id>/output` until the
  job completes (5 min default timeout).

## Environment variables

| Variable                  | Default                  | Description                                              |
| ------------------------- | ------------------------ | -------------------------------------------------------- |
| `THRUK_BASE_URL`          | `http://localhost/thruk` | Thruk URL (no trailing slash)                            |
| `THRUK_API_KEY`           | *(required)*             | `X-Thruk-Auth-Key` header                                |
| `THRUK_AUTH_USER`         |                          | Impersonation user (superuser key only)                  |
| `THRUK_VERIFY_SSL`        | `true`                   | Set `false` for self-signed certs                        |
| `THRUK_TIMEOUT`           | `30`                     | HTTP timeout in seconds                                  |
| `THRUK_DEFAULT_BACKENDS`  |                          | CSV of default backend names (federated Thruk)           |

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
- No direct push to `main`: branch \u2192 PR \u2192 squash merge.
- Any new tool must come with a `respx`-mocked unit test in `tests/test_tools.py`
  and an entry in `catalog/tools.json` (Docker MCP Registry contract).
- CI gate: `ruff`, `ruff format --check`, `mypy`, `pytest` with **80 %
  coverage minimum**.

## References

- Thruk REST API: <https://www.thruk.org/documentation/rest.html>
- Thruk REST commands: <https://www.thruk.org/documentation/rest_commands.html>
- MCP spec: <https://spec.modelcontextprotocol.io/>
- Inspired by: <https://github.com/lausser/omd-mcp> (initial proof-of-concept)

## License

MIT — see [LICENSE](LICENSE).
