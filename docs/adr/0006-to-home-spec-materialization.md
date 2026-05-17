# ADR-0006: `to_home/` spec-dir materialization

**Status:** Accepted (2026-05-17).
**Supersedes:** the `dot_claude/` convention introduced by F-DC1 (kept
alive one release as the deprecated fallback).
**Source:** `scitex-lead` FUTURE doc
`~/proj/scitex-lead/GITIGNORED/FUTURE/sac-to_home-refactor.md`.

## Context

The `dot_claude/` materialization (ADR-0003 sibling logic, codified in
`runtimes/_dot_claude.py`) mixes two unrelated purposes in one
directory:

- Four well-known leaf files (`CLAUDE.md`, `.mcp.json`, `.env`,
  `state.md`) land at the workspace root.
- Everything else under `dot_claude/` is mirrored into
  `<workdir>/.claude/<rel>`.

That fragmentation surfaced four concrete pain points during the
2026-05-17 SIF migration session (full log in lead's FUTURE doc; see
ADR-0005 for the SIF context):

1. **File-bind auto-create breaks.** sac's apptainer bind pre-create
   only handles directory targets. A spec wanting `.gitconfig` at
   `/home/agent/.gitconfig` had to either pre-touch a placeholder
   file (workaround) or accept the "source is file, dest is dir"
   apptainer error.
2. **Workdir == `$HOME`** caused `git status` to report
   `.claude/` materialization changes as untracked on every restart,
   polluting every diff.
3. **`state.md` semantic collision** with the `GITIGNORED/` pattern
   used elsewhere in the lead's working tree.
4. **Secrets were threaded through ad-hoc `spec.apptainer.binds`** with
   file-vs-directory quirks instead of being a first-class
   per-agent layout concern.

## Decision

Introduce `to_home/` as the canonical spec-dir layout for
agent-`$HOME` materialization. The rule becomes explicit:

> **Everything under `<spec_dir>/to_home/` is mirrored 1:1 into the
> agent's container `$HOME` (`= runtime/<name>/home/` on the host) on
> every start.**

No leaf-vs-mirror branching. The path you see under `to_home/` is the
path the agent sees relative to `$HOME`.

### Per-entry semantics (encoded in `runtimes/_to_home.py`)

| Entry type | Action |
|---|---|
| `CLAUDE.md`, `state.md` | Marker-protected merge — source wrapped in Start/End markers; user content past the End marker preserved; malformed existing markers hard-abort (`WorkspaceCLAUDEMarkerError`). |
| `.env` | Full overwrite, then `chmod 0600`. |
| Other regular file | Full overwrite (`shutil.copy2`, then `${metadata.*}` / `${ENV}` interpolation when text-decodable). |
| Directory | Recursed; structure preserved. |
| Symlink | Preserved verbatim. Target string passes through unchanged whether relative or absolute. |

### Spec schema change

- New field `AgentConfig.to_home: str = "./to_home"` (default
  auto-discovers `to_home/` next to `spec.yaml`).
- Loader parses the YAML key `spec.to_home` with the same default.
- Validator accepts `to_home` as a known top-level spec key.

### Workdir / `$HOME` separation

Container layout after migration:

- `/home/agent/` = mirror of `to_home/`.
- `/work` = bind-mounted source repo (canonical workdir).
- `/home/agent/proj/<pkg>/` = symlink to `/work` for shell muscle
  memory (`cd ~/proj/<pkg>` still works).

The symlink is created by a per-spec `startup_command`:

```yaml
startup_commands:
  - command: mkdir -p $HOME/proj && ln -sfn /work $HOME/proj/<pkg>  # our convention
```

This is the **convenience-as-affordance** pattern from the lead's
FUTURE doc: the abstract path (`/work`) is canonical and
host-layout-free; the friendly path (`~/proj/<pkg>`) is a zero-coupling
symlink that preserves muscle memory and existing prompts.

### Deprecation policy

- A spec MUST NOT carry **both** `to_home/` and `dot_claude/` next to
  `spec.yaml`. The agent-start path raises a `RuntimeError` if both
  exist. No silent merging — that's the data-loss pattern we're
  explicitly avoiding.
- A spec with **only** `dot_claude/` continues to work for one release,
  emitting a `DeprecationWarning` at start.
- `runtimes/_dot_claude.py` is **not deleted** in this change. It will
  be removed in a follow-up after the in-tree specs are migrated.

## Consequences

**Positive:**

- File binds for `.gitconfig`, `.ssh`, `.config/gh`, etc. go away —
  they live as files / dirs under `to_home/` and materialize at the
  correct path automatically.
- Workdir (`/work`) and `$HOME` (`runtime/<name>/home/`) are
  cleanly separated; restart-time materialization no longer leaks
  into the source-tree `git status`.
- Per-agent secrets live in `to_home/secrets/` (with a per-dir
  `.gitignore` of `*` + `!.gitkeep` to keep contents out of the
  dotfiles repo) — first-class instead of bind-threaded.
- The marker-protection contract for `CLAUDE.md` / `state.md` is
  **identical** to `dot_claude/` (shared helpers from
  `_dot_claude.py`). No regression in the safety guarantee.

**Negative:**

- One-release window where two layouts coexist in the runtime path.
  Mitigated by the both-present hard-fail and the
  DeprecationWarning on legacy-only.
- Existing specs need a one-time migration commit. Six in-tree
  specs (`proj-scitex-*` and `proj-paper-scitex-clew`) are migrated
  in the companion dotfiles PR.

## Implementation notes

- Marker-protection helpers (`_validate_marker_invariants`,
  `_extract_user_tail`, `END_MARKER`, `WorkspaceCLAUDEMarkerError`)
  are imported from `_dot_claude` so both modules share one source
  of truth on the merge contract.
- `materialize_to_home(spec_dir, workspace_home)` is the
  config-free low-level entrypoint (used by tests and any future
  caller that doesn't have a full `AgentConfig`).
- `deploy_to_home(config, workspace_home)` is the
  config-aware variant (used by the agent-start path) and runs
  `${metadata.*}` / `${ENV}` interpolation over text files.

## Related work

- `runtimes/_dot_claude.py` — deprecated predecessor (kept for one
  release).
- ADR-0003 — runtime home directory layout (`runtime/<name>/home/`
  bind target).
- ADR-0005 — SIF-mode migration (the immediate trigger for this
  refactor; documents the file-bind pain in detail).
- Lead memory `feedback_scitex_state_tracking_policy.md` — the
  "track everything except `runtime/`" rule pairs cleanly with the
  separated `to_home/` layout.
