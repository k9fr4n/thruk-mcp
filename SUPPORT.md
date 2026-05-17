# Support Policy

## Versioning

This project follows [Semantic Versioning 2.0](https://semver.org/):

- **MAJOR** bumps may break tool signatures, env vars, MCP resource URIs or the catalog schema.
- **MINOR** bumps add tools, resources, prompts or env vars without breaking existing callers.
- **PATCH** bumps are bug fixes only.

Breaking changes are always documented in [CHANGELOG.md](CHANGELOG.md) and
[UPGRADING.md](UPGRADING.md).

## Supported Python versions

| Version | Status | Notes |
| ------- | ------ | ----- |
| 3.12    | ✅ Tier 1 | Primary CI target (coverage uploaded from here) |
| 3.11    | ✅ Tier 1 | Full test matrix |
| 3.10    | ✅ Tier 1 | Full test matrix |
| 3.9     | ❌ Unsupported | EOL upstream October 2025 |
| 3.13    | 🟡 Best effort | Not in CI matrix yet, expected to work |

When a Python version reaches end-of-life upstream we drop it in the next
MINOR release after a one-version deprecation notice in `CHANGELOG.md`.

## Supported Thruk versions

Developed and tested against **Thruk 3.x** (REST API v1 and v2).

| Version | Status |
| ------- | ------ |
| 3.x     | ✅ Officially tested |
| 2.40+   | 🟡 Best effort — most endpoints documented under `/r/v1/...` are stable |
| < 2.40  | ❌ Unsupported — lacks several endpoints we rely on (e.g. `/r/sites`) |

## Supported MCP clients

Any client compliant with the MCP specification works. Specifically verified:

- Claude Desktop (stdio + Streamable-HTTP)
- VS Code MCP extension
- LibreChat
- Cursor
- Docker MCP Gateway (default catalog or custom local catalog)

## Security

Report security issues privately by emailing fsallet@ecritel.net rather than
opening a public issue. We will acknowledge within 7 days and aim to ship a
fix within 30 days of confirmation.

Security-relevant features (read-only mode, tool allowlist, audit log) are
described in the [Security section of the README](README.md#security).

## Release cadence

No fixed schedule. Releases ship when meaningful changes accumulate; expect
roughly one MINOR every 4–6 weeks while the project is under active
development.

## Getting help

- **Bugs / feature requests**: open a [GitHub issue](https://github.com/k9fr4n/thruk-mcp/issues).
- **Questions**: GitHub Discussions or the same issue tracker.
- **Commercial support**: not offered; contributions and forks welcome.
