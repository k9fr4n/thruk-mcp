# Contributing

Thanks for considering a contribution! This project is small enough that the
rules can fit on one page.

## Quick setup

```bash
git clone https://github.com/k9fr4n/thruk-mcp.git
cd thruk-mcp
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

Run the full checks the way CI does:

```bash
ruff check src tests
ruff format --check src tests
mypy src
pytest -v --cov=thruk_mcp --cov-fail-under=80
```

## Branching and PR flow

- **No direct push to `main`.** All changes go through a PR.
- Branch prefixes: `feat/`, `fix/`, `chore/`, `docs/`, `refactor/`, `test/`.
- We squash-merge. Make your commit message look like the final PR title.
- PR titles follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(server): ...`, `fix(client): ...`, `chore(ci): ...`, etc.

## Adding a new MCP tool

1. Define the tool in `src/thruk_mcp/server.py` inside `build_server()`.
   - Use `snake_case` and the `thruk_` prefix.
   - Type every parameter; no `**kwargs`.
   - Docstring is LLM-facing — keep it concise and unambiguous.
   - Surface `ThrukError` messages verbatim; never swallow them.
2. **List tools must** accept `limit`, `offset`, `sort`, `columns` and use
   `_list_params()` + a `DEFAULT_*_COLUMNS` constant.
3. **Write tools must** be added to `WRITE_TOOLS` in `server.py` so that
   `THRUK_READ_ONLY` and the audit log apply to them.
4. Add an entry in `catalog/tools.json` (one line per tool).
5. Add a `respx`-mocked routing test in `tests/test_tools.py` asserting the
   method, URL path and key params.
6. Run the checks listed above. Coverage gate is **80 %**.

## Adding a new env var

Any new `THRUK_*` env var must appear in three places (the CI does not enforce
this but reviewers will):

- `src/thruk_mcp/config.py` — added as a field on `ThrukConfig` and parsed in
  `from_env()`.
- `.env.example` — with a comment describing it.
- `catalog/server.yaml` — in `config.env` *and* `config.parameters` so the
  Docker MCP Toolkit UI can render it.

## Releasing (maintainers)

1. Open a `chore/release-X.Y.Z` branch.
2. Bump `version` in `pyproject.toml` and `__version__` in
   `src/thruk_mcp/__init__.py`.
3. Add a `CHANGELOG.md` section dated to release day, plus a new compare
   link at the bottom.
4. Note any breaking change in `UPGRADING.md`.
5. PR → review → squash merge.
6. From `main`: `git tag -a vX.Y.Z -m '...'` then `git push origin vX.Y.Z`.
7. `gh release create vX.Y.Z --title '...' --notes '...'`.

The tag triggers `.github/workflows/release.yml` which builds and pushes
`ghcr.io/k9fr4n/thruk-mcp:{X.Y.Z,X.Y,latest}` (multi-arch, provenance, SBOM).

## Reporting bugs

Open an issue with:

- Thruk version (`/thruk/r/processinfo`)
- `thruk-mcp --help` (covers version + transport mode)
- The exact tool call that misbehaved and the error message
- Whether the same call works with `curl` against Thruk

## Code of conduct

Be respectful. Disagreements are fine, personal attacks are not.
