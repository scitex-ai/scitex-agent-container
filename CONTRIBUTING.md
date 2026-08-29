# Contributing to scitex-agent-container

`sac` is a thin, declarative container wrapper for agents — Docker /
Podman / Apptainer underneath, an SDK-persistent runner inside.
Contributions follow the SciTeX ecosystem conventions; the canonical
reference lives under `~/.claude/skills/scitex/general/`.

## Quick path

```bash
git clone https://github.com/ywatanabe1989/scitex-agent-container.git
cd scitex-agent-container
pip install -e ".[dev]"     # pulls fastmcp + pytest-asyncio so the full
                            # test suite (including tests/.../_mcp/ and
                            # async A2A inbox tests) runs out of the box.
# Need every optional surface (telegram + slurm + docs as well)?
#   pip install -e ".[all]"
pytest tests/
scripts/install-git-hooks.sh   # one-time: points core.hooksPath at .githooks/
                               # so the repo's hooks ACTUALLY RUN
```

## Git hooks

`scripts/install-git-hooks.sh` sets `core.hooksPath` to the version-controlled
`.githooks/`. Run it once per clone. `.git/hooks` is untracked, so a fresh clone
otherwise starts with no hooks and no way to notice.

| hook | what it runs |
|---|---|
| `.githooks/pre-commit` | the pre-commit framework (`.pre-commit-config.yaml`) — fast, bounded, deterministic checks |
| `.githooks/pre-push`   | `ruff --select F401,F811` + a direct-push guard on `main`/`master` |

**Pre-commit is not a gate. CI is the gate.** Pre-commit runs fast, bounded,
deterministic checks and **does not run the test suite** — that is scitex-dev's
fleet policy (`_skills/general/05_development/15_pre-commit-policy.md`), enforced
mechanically by audit rule **PS-HOOK-001** (severity E). Tests belong in CI,
which already runs the full suite on three Python versions on every push.
Pre-commit's only job is saving you a wasted CI cycle.

Until 2026-07-15 none of this actually executed: the framework's shim was never
installed, and `core.hooksPath` pointed at the absolute `.git/hooks`, which
shadowed `.githooks/`. The conventions were advertised as hook-enforced and were
enforced by nothing — which is why the CI ruff job exists at all
(see the comment at the top of `.github/workflows/lint.yml`).

## Branch model

- `main` — release-only; tags cut from here.
- `develop` — default integration branch; PRs from feature branches land here.
- `feat/<short-name>` / `fix/<short-name>` — feature branches; merge into
  `develop` once tests are green.

The release flow `develop → main` is gated by CI (Test, SciTeX Quality,
Docs, SDK runtime smoke). See `.github/workflows/`.

### A robot merges your PR. Nobody reviews it.

Say it plainly, because this document used to say "reviewer-approved" and no
reviewer exists: `.github/workflows/auto-merge-to-develop.yaml` is a cron sweep
that runs about every 15 minutes and merges green PRs targeting `develop` with
`gh pr merge --admin`. There is no human in that loop. A PR that goes green at
02:00 is typically merged by 02:15 by a workflow, not a person.

**To stop that, put a hold on the PR — a hold the automation can actually read:**

- add a **`hold`** or **`do-not-merge`** label (one click in the GitHub UI), or
- convert the PR to a **draft**.

The sweep refuses a held PR on every tick and says so in its run log, naming the
PR and the marker. A hold spends no merge budget, so holding one PR never delays
anyone else's. Remove the marker and it merges on the next tick — nothing else to
do. Deciding not to merge something and telling only your teammates is not a
hold; on 2026-08-12 a PR held exactly that way was merged anyway, because nothing
in the repository knew.

**Every automated merge leaves a comment on the PR before it merges**, naming the
workflow and its reasons. That comment is the audit trail: every agent, workflow
and human here acts through the same GitHub account, so the `merged_by` name on a
merged PR cannot tell you whether anybody read the diff. If you find such a
comment, the answer is that nobody did.

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
ruff check src/ tests/                          # local edit-time lint
scitex-dev ecosystem audit-all scitex-agent-container
```

A narrower **`ruff check --select F401,F811`** also runs as a pre-push
gate (`.githooks/pre-push`, armed by
`scripts/install-git-hooks.sh`) and as a CI job
(`.github/workflows/lint.yml`) on PRs + pushes to `develop`/`main`.
Scope is intentionally narrow (unused-import + redefinition only) — the
local edit hook stopped autofixing F401 so subagent multi-step edits
aren't sabotaged mid-flight; the pre-push + CI gates are what catch a
genuinely-unused import before it lands. Tighten the `--select` set
once the broader pyproject.toml ruleset baseline (~100 pre-existing
E/F/W/I violations) is triaged.

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
- **State**: per-host PostgreSQL, reached through `scitex_dev.store`; the
  `_state/state_db*.py` modules are the accessors. JSON registry support
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
