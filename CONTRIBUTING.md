# Contributing to scitex-agent-container

`sac` is a thin, declarative container wrapper for agents — Docker /
Podman / Apptainer underneath, an SDK-persistent runner inside.
Contributions follow the SciTeX ecosystem conventions; the canonical
reference lives under `~/.claude/skills/scitex/general/`.

## Quick path

```bash
git clone https://github.com/ywatanabe1989/scitex-agent-container.git
cd scitex-agent-container
pip install -e ".[dev,mcp]"
pytest tests/
```

## Branch model

- `main` — release-only; tags cut from here.
- `develop` — default integration branch; PRs from feature branches land here.
- `feat/<short-name>` / `fix/<short-name>` — feature branches; merge into
  `develop` once tests are green and reviewer-approved.

The release flow `develop → main` is gated by CI (Test, SciTeX Quality,
Docs, SDK runtime smoke). See `.github/workflows/`.

## Running tests

```bash
pytest tests/                                  # full suite (default)
pytest tests/scitex_agent_container/_mcp/      # MCP surface only
pytest tests/scitex_agent_container/runtimes/  # container / SDK runtime
pytest -m integration                           # opt-in expensive integration
```

Tests must mirror src layout: every `src/scitex_agent_container/<path>/<file>.py`
has a matching `tests/scitex_agent_container/<path>/test_<file>.py`. The
`scitex-dev ecosystem audit-project` linter enforces this.

## Linting and audits

```bash
scitex-dev ecosystem audit-all scitex-agent-container
```

Covers CLI hygiene (mutating verbs need `--dry-run`/`--yes`; help blocks
must include `Example:`), MCP tool naming, project structure (CHANGELOG,
CONTRIBUTING, examples/, mirror tests), Python API exports.

## Coding conventions

- **Container-only.** No bare-metal runtime branches. Container engines
  are `docker`, `podman`, `apptainer`. SLURM and CLI/TUI paths were
  removed in F-CS17.
- **CLI shape**: noun-verb leaves under `cli_pkg/<noun>_group.py`. Renames
  hard-error per scitex CLI §5.
- **Auth**: `SAC_ANTHROPIC_API_KEY` is sac's namespaced handoff; the
  runner translates it at the SDK transport boundary. Do not synthesise
  `ANTHROPIC_API_KEY` on the host side — that's the operator's choice.
- **State**: SQLite at `~/.scitex/agent-container/runtime/state.db`; rows
  written through `_state/state_db.py` helpers. JSON registry support
  exists only via `sac db migrate` for legacy import.
- **Comments**: write *why*, never *what*. No comments on simple lines.

## Filing changes

1. Open or pick up an F-CS item in `GITIGNORED/FEATURE_REQUESTS.md` (gitignored
   local backlog) or a GitHub issue.
2. Branch off `develop`.
3. Land tests alongside code (TDD strongly preferred for new lifecycle code).
4. Update `CHANGELOG.md` `[Unreleased]` section.
5. PR into `develop`. CI must be green.

## Release

`develop → main` PR. Tag from `main` after merge. `pyproject.toml`
version bump in the same PR. CI publishes to PyPI on tag push.

## License + CLA

Code is AGPL-3.0-only. PRs require CLA acknowledgement (the bot will
prompt on first contribution). See `LICENSE` and `signatures/cla.json`.
