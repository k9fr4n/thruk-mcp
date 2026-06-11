# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.10.1] - 2026-06-11

### Fixed
- `thruk_alert_heatmap` / `thruk_notification_heatmap` returned all-zero buckets
  on busy windows: an ungrouped `count(*)` collapses to a single `{"cnt": N}`
  object (not a list) on Thruk's normal path, but `_sum_cnt` only summed lists.
  Also fixes the same latent under-count in `thruk_reliability_report`'s
  `total_events`. Regression from #312 (#314).

## [1.4.0] - 2026-05-25

### Added
- `thruk_host_availability` and `thruk_service_availability` tools: retrieve SLA/availability
  data from Thruk's `/availability` endpoint (#171, #174).

### Fixed
- **Security**: scrub Thruk auth headers and API keys from error messages and log output (#149).
- **Security**: `thruk_query` validates the HTTP method and rejects path traversal (`..`)
  in the path argument (#123).
- **Security**: block write methods of `thruk_query` and `thruk_run_background_query`
  when `THRUK_READ_ONLY=true` (#138).
- **Security**: URL-encode path segments to prevent host/service name injection.
- Disable transport-level retries to avoid double-retry amplification with Thruk (#150).
- `TTLCache`: switch to `OrderedDict` for O(1) eviction; add size cap to prevent unbounded
  memory growth (#91, #148).
- Configure explicit `httpx.Limits` / `httpx.Timeout` on the shared `AsyncClient` (#144).
- Remove the module-level `_client` global from `server.py` (#143).
- Register MCP resources and prompts on the low-level `Server` object (#145).
- Paginate `/hosts` lookup in `_resolve_hosts_to_regex` to avoid silent 1 000-host
  truncation (#142).
- Parallelise per-id deletions in `thruk_delete_active_downtimes` (#141).
- Replace deprecated `datetime.utcfromtimestamp()` in `thruk_alert_heatmap` (#140).
- Use timezone-aware UTC timestamps for Thruk filter parameters (#139).
- `ThrukConfig.__repr__` exposed the API key in logs and tracebacks (#122).
- `get_event_loop()` replaced with `get_running_loop()` in `run_background` (client).
- `ValueError` in `thruk_delete_downtimes_by_filter` escaped `call_tool` and triggered
  an MCP protocol error (#71).
- `thruk_query` POST/DELETE calls are now correctly appended to the audit log (#73).
- Warn at startup when `THRUK_VERIFY_SSL=false` (#74).

### Changed
- Split monolithic `server.py` (3 000+ lines) into a `tools/` sub-package (#147).
- Centralise `json.dumps` response builder into a single helper (#146).
- Unify `_TOOL_DISPATCH` and `_TOOL_SCHEMAS` into a single `ToolSpec` registry (#85).
- Consolidate duplicated state maps into `constants.py` (#81).
- Replace sequential `await` chains with `asyncio.gather()` in five tool functions (#75).
- O(n) sliding-window algorithm in `thruk_concurrent_failures` (#86).
- Pin dependency upper bounds for `mcp` and `httpx`; add Dependabot config (#78).
- Pin GitHub Actions steps to commit SHAs instead of mutable tags (#77).
- Remove `continue-on-error` from `integration.yml` to surface failures explicitly (#151).
- Add `__all__` exports to all public modules (#88).
- Enable `disallow_untyped_defs` in mypy; fix all surfaced typing issues (#89).
- Extract shared aggregation helper for `thruk_top_noisy_hosts` / `thruk_top_noisy_services`
  (#84).
- Parametrize duplicated log-family tool tests (#90).
- Add missing unit tests: `thruk_concurrent_failures`, `thruk_flap_summary`, HTTP 429
  retry, timeout retry (#83).

## [1.3.0] - 2026-05-22

### Changed
- Docker MCP Catalog entry (`catalog/tools.json`) updated with `thruk_concurrent_failures`
  tool metadata (#93).

## [1.2.0] - 2026-05-21

### Added
- `thruk_alert_heatmap`: alert counts grouped by configurable time bucket over a window.
- `thruk_recurring_problems`: hosts/services that generated repeated alerts over a window.
- `thruk_concurrent_failures`: detect windows where multiple hosts failed concurrently (#54).
- `thruk_oldest_problems`, `thruk_stale_acks`, `thruk_unacked_critical`,
  `thruk_problems_by_hostgroup`: semantic problem-management tools (#52).
- `thruk_top_noisy_hosts` and `thruk_top_noisy_services`: rank noisiest items by alert
  count (#63).
- `since` / `until` parameters on noisy/flap tools replacing `hours` / `window_hours`
  (#68).

### Changed
- Remove spill-to-workdir mechanism (#62).

### Fixed
- ruff format applied to `server.py` and `test_semantic_tools.py` (#65).

## [1.1.2] - 2026-05-20

### Fixed
- Filter builder: extract AND scalar leaves from `q=` when an OR subtree is present (#61).

## [1.1.1] - 2026-05-20

### Fixed
- Filter builder: strip outer parentheses on the root `q=` expression.

## [1.1.0] - 2026-05-20

### Added
- Structured AND/OR filter tree for all list tools (#60).
- Custom-variable (`custom_var`) filtering on all list/alert/notification tools (#39).
- Hostgroup filter on `thruk_problems`, `thruk_list_notifications`,
  `thruk_recent_events` (#44).

### Fixed
- Accept numeric strings for state filter in `list_hosts` / `list_services` (#59).
- Expand `/alerts` and `/notifications` aliases client-side (#41).
- Correct Thruk REST verb for downtime deletion (#37).
- Delete ALL active downtimes + explicit host-downtime cleanup (#32).
- Route background job poll through REST prefix `/r/` (#31).
- Graceful per-backend fallback on federation failure (#30).
- Surface `ThrukError` as tool content instead of raising (#29).
- Switch to POST for log queries when params exceed 3 800 chars (#48).
- Resolve hostgroup to `host_name[regex]` for log-family tools (#47).
- Add `contact_name` / `command_name` to notification default columns (#46).

## [1.0.0] - 2026-05-17

First stable release. The API surface (29 tools, 5 resources, 3 prompts,
14 env vars) is now committed to semantic versioning: future MAJOR bumps
will be announced and documented ahead of time.

### Added

- `SUPPORT.md`: explicit Python / Thruk / MCP-client support matrix,
  security reporting channel, release cadence.
- `CONTRIBUTING.md`: development setup, PR conventions, tool/env-var
  contribution checklists, release process for maintainers.
- `UPGRADING.md`: per-MINOR migration notes covering 0.2.0 \u2192 1.0.0.
- `.github/workflows/pypi.yml`: PyPI publish via trusted OIDC publisher
  on every published GitHub release (no token required once the pending
  publisher is configured on pypi.org).
- `.github/workflows/integration.yml`: nightly live-Thruk integration
  workflow that boots an OMD demo container, generates an API key, and
  runs `pytest -m integration` against it. `continue-on-error: true`
  for now so the badge stays green while the upstream demo image is in
  flux.
- `compose.test.yml`: docker-compose definition for the OMD demo
  Thruk used by the integration workflow and by local maintainers.
- `scripts/get-test-api-key.sh`: helper to mint a superuser API key
  inside the running OMD container.
- `tests/integration/test_live.py`: three smoke tests
  (`/processinfo`, `/hosts`, `/hosts/stats`) gated on
  `pytest.mark.integration` and skipped by default unless
  `THRUK_API_KEY` is set.

### Changed

- `pytest` default invocation now passes `-m 'not integration'` so the
  standard `pytest` command stays fast and offline; the integration
  workflow overrides with `-m integration`.

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

[Unreleased]: https://github.com/k9fr4n/thruk-mcp/compare/v1.10.1...HEAD
[1.10.1]: https://github.com/k9fr4n/thruk-mcp/compare/v1.10.0...v1.10.1
[1.0.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.5.0...v1.0.0
[0.5.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/k9fr4n/thruk-mcp/releases/tag/v0.1.0
