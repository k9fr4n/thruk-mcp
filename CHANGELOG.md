# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-05-17

### Added

- **Security & multi-tenant knobs**:
  - `THRUK_READ_ONLY=true` strips every write tool from the server
    (acknowledge, schedule_*_downtime, recheck, delete_*,
    run_background_query). Read tools remain available.
  - `THRUK_ENABLED_TOOLS=thruk_list_*,thruk_problems` restricts the
    exposed tool surface via fnmatch wildcards. Empty = no filter.
  - `THRUK_AUDIT_LOG=true` (default) emits one JSON line per write
    tool invocation on the `thruk_mcp.audit` logger (stderr). Sensitive
    keys (`api_key`, `password`, `token`) redacted as `***`. Payload:
    `ts`, `tool`, `user`, `args`, `target`, `status`, `error`.
  - `THRUK_MAX_CONCURRENT=N` caps in-flight HTTP requests with an
    `asyncio.Semaphore` to protect the Thruk core from a looping LLM.
- New module `src/thruk_mcp/audit.py`: `configure()`, `audited()`
  decorator, `_redact()` helper.
- 9 new security tests; coverage now **84.02 %** (was 82.10 %).
- Codecov badge, Python versions badge and ghcr.io badge in README.
- `catalog/server.yaml` declares the 4 new env vars in `config.env`
  and `config.parameters` so the Docker MCP Toolkit UI renders them.
- Strict Codecov upload in CI (`fail_ci_if_error: true`, scoped token,
  `flags: unittests`).

### Fixed

- README and CHANGELOG: 6 literal `\u2014` / `\u2192` escape sequences
  left over from earlier heredoc commits replaced with real Unicode
  characters (em-dash, right-arrow).

## [0.4.0] - 2026-05-17

### Added

- Comprehensive test suite: **63 passing tests, 82 % coverage**
  (was 15 tests, ~50 %).
  - `tests/test_tools.py` covers the 29 MCP tools (URL / method /
    key params for each), including a regression test for the v0.2
    acknowledge payload-key bug.
  - `tests/test_resources.py` covers the 5 MCP resources.
  - `tests/test_prompts.py` covers the 3 MCP prompts.
  - `tests/test_config.py` covers `ThrukConfig.from_env()`.
  - `tests/test_run_background.py` covers the 302 → 200 polling cycle
    and the pass-through fallback.
- `mypy` type-checking baseline (`warn_redundant_casts`,
  `warn_unused_ignores`, `warn_unreachable`, `no_implicit_optional`,
  `check_untyped_defs`). 0 errors on `src/`.
- `ruff format` integrated alongside `ruff check`.
- `.pre-commit-config.yaml` (ruff + ruff-format + mypy + standard
  pre-commit hooks).
- Coverage gate in CI: `pytest --cov-fail-under=80`.
- Codecov upload on Python 3.12 CI matrix entry.
- `[tool.coverage.*]` configuration with branch coverage and sensible
  excludes. `integration` pytest marker registered for future live
  tests.

### Changed

- **API cleanup** (small breaking change): `thruk_query` and
  `thruk_run_background_query` arguments are now `params: dict` and
  `data: dict` instead of `params_json: str` / `data_json: str`. The
  previous JSON-string parameters were impossible to call through
  FastMCP because pydantic auto-decodes JSON-looking strings before
  reaching the function. Migration: pass a dict literal instead of a
  JSON string.

## [0.3.0] - 2026-05-17

### Added

- **Connection retries** via `httpx.AsyncHTTPTransport(retries=3)` for
  DNS / TCP / TLS handshake failures.
- **HTTP retries with exponential backoff + jitter** for 429 and 5xx
  responses (cap 5 s, configurable). 4xx are not retried.
- **Async-safe TTL cache** (`thruk_mcp.cache.TTLCache`, default 15 s)
  wired to slow-moving endpoints: `/sites`, `/processinfo`,
  `/*/stats`, `/*/totals`, `/contacts`, `/contactgroups`,
  `/timeperiods`, `/commands`. Per-call override via `cache_ttl=`.
- **`ThrukClient.get_all()`** — async paginator over a list endpoint
  using `limit`/`offset`, with `hard_limit` safety net (default 50k).
- **`ThrukClient.run_background()`** + new tool
  `thruk_run_background_query` — wrap Thruk's `?background=1` flow
  and poll `/thruk/jobs/<id>/output` (302 vs. 200) until completion.
- **5 MCP Resources** — `thruk://hosts/{name}`,
  `thruk://services/{host}/{service}`, `thruk://hostgroups/{name}`,
  `thruk://problems`, `thruk://stats`. Clients with a resource browser
  (Claude Desktop, VS Code, ...) can open Thruk objects like files.
- **3 MCP Prompts** — `investigate_alert(host, service?)`,
  `schedule_maintenance(target, duration_minutes, kind)`,
  `diagnose_flapping(host, service)`. Pre-canned slash-commands for
  the most common ops workflows.
- 12 new tests (cache TTL semantics, get_all pagination, retry on
  503/4xx, cache hit). Suite now 15 passing.

### Changed

- README rewritten: \"What is exposed\" section (29 tools / 5 resources /
  3 prompts) and \"Robustness\" section. The stale v0.1 tools table was
  removed.

## [0.2.0] - 2026-05-17

### Added

- **Log / history tools** for incident investigation:
  `thruk_list_logs`, `thruk_list_alerts`, `thruk_list_notifications`,
  `thruk_recent_events`, `thruk_get_downtime`.
- **Comprehensive downtime management**:
  `thruk_schedule_host_services_downtime` (all services of a host),
  `thruk_schedule_propagated_host_downtime` (parent + child hosts, optionally
  triggered), `thruk_schedule_hostgroup_downtime`,
  `thruk_schedule_servicegroup_downtime`,
  `thruk_delete_active_downtimes`, `thruk_delete_downtimes_by_filter`
  (bulk delete via `del_downtime_by_{host_name,hostgroup_name,start_time_comment}`).
- **Pagination, sort and tight default columns** on every list-style tool:
  new `offset`, `sort`, `columns` arguments. Module-level
  `DEFAULT_*_COLUMNS` constants document the curated subsets. Pass
  `columns=""` to opt out and get every column (v0.1 behaviour).
- Unit tests for `_list_params` (8 passing tests total).

### Changed

- All list tools (`thruk_list_hosts`, `thruk_list_services`,
  `thruk_list_hostgroups`, `thruk_list_servicegroups`, `thruk_problems`,
  `thruk_list_downtimes`, `thruk_list_comments`, plus the 4 log-family
  tools) now return a tight default subset of columns to dramatically
  reduce LLM token consumption. **Breaking** for callers that relied on
  every field being returned: pass `columns=""` to restore.
- `limit` is now clamped to `1..1000` on every list tool.

### Fixed

- `thruk_acknowledge` payload keys corrected to match the Thruk REST
  contract (`sticky_ack`, `send_notification`, `persistent_comment`).
  The previous keys (`sticky`, `notify`, `persistent`) were silently
  ignored by the core, meaning the `notify=False` flag never actually
  suppressed notifications.

## [0.1.0] - 2026-05-17

### Added

- Initial release.
- 17 MCP tools for the Thruk REST API: hosts, services, groups,
  downtimes (schedule/delete), comments, sites, stats, problems,
  acknowledge / remove ack, force recheck, plus a `thruk_query`
  escape hatch for any other endpoint.
- Async `httpx` client with native multi-backend (federated Thruk
  sites) support.
- Two transports: stdio (default) and Streamable-HTTP (`--listen PORT`).
- Multi-stage Dockerfile, non-root user, ghcr.io publishing on tag.
- Docker MCP Gateway compatibility (`catalog/server.yaml`,
  `catalog/tools.json`, stdio default).
- GitHub Actions CI (ruff + pytest matrix on 3.10/3.11/3.12 + Docker
  build) and release workflow (multi-arch image with provenance + SBOM).

[Unreleased]: https://github.com/k9fr4n/thruk-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/k9fr4n/thruk-mcp/releases/tag/v0.1.0
