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

## Tools

| Tool                          | Purpose                                            |
| ----------------------------- | -------------------------------------------------- |
| `thruk_list_hosts`            | List/filter hosts                                  |
| `thruk_get_host`              | Detail of a single host                            |
| `thruk_list_services`         | List/filter services                               |
| `thruk_get_service`           | Detail of a single service                         |
| `thruk_list_hostgroups`       | List host groups                                   |
| `thruk_list_servicegroups`    | List service groups                                |
| `thruk_problems`              | Current unhandled host & service problems          |
| `thruk_stats`                 | Aggregated host/service stats                      |
| `thruk_list_downtimes`        | Active or all scheduled downtimes                  |
| `thruk_list_comments`         | Comments (incl. ack flags)                         |
| `thruk_sites`                 | List configured Thruk backends                     |
| `thruk_schedule_downtime`     | Schedule downtime on host or service               |
| `thruk_delete_downtime`       | Delete a downtime by id                            |
| `thruk_acknowledge`           | Acknowledge a problem                              |
| `thruk_remove_acknowledgement`| Remove an acknowledgement                          |
| `thruk_recheck`               | Schedule an immediate (forced) check               |
| `thruk_query`                 | Raw call to any Thruk REST endpoint                |

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
ruff check src tests
pytest -v
```

## References

- Thruk REST API: <https://www.thruk.org/documentation/rest.html>
- Thruk REST commands: <https://www.thruk.org/documentation/rest_commands.html>
- MCP spec: <https://spec.modelcontextprotocol.io/>
- Inspired by: <https://github.com/lausser/omd-mcp> (initial proof-of-concept)

## License

MIT — see [LICENSE](LICENSE).
