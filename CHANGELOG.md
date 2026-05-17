# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/k9fr4n/thruk-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/k9fr4n/thruk-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/k9fr4n/thruk-mcp/releases/tag/v0.1.0
