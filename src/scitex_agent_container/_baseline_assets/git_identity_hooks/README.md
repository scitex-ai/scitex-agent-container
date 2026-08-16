# Git-identity guard hooks (CLA-author allowlist)

Canonical, version-controlled, tested source for the PreToolUse Bash
hook that keeps a non-CLA-allowlisted commit author from silently
reaching CI. Sibling in spirit to `../claude_worktree_hooks/`: the
authoritative copy lives here in the package and is **propagated** into
the fleet baseline that `runtimes/_to_home.py` materializes into every
agent's `$HOME/.claude/`.

## Why

Evidenced incident (scitex-hpc, 2026-07-05): an agent's PR went fully
GREEN on real CI (audit / docs / import-smoke / pytest 3.11-3.13) but
the **required `CLAssistant` check FAILED** and blocked the merge. The
commits were authored `agent@scitex-hpc`, which maps to no GitHub
account, and the CLA allowlist (`bot*`, `ywatanabe1989`) rejected it.
Force-push is hook-blocked, so the only remedy was re-creating the whole
tree as a fresh commit authored by the allowlisted identity
(`ywatanabe@scitex.ai` = `ywatanabe1989`) on a NEW branch — pure,
avoidable rework, discovered only AFTER a full CI cycle.

Root cause: the container's git author is meant to default to the
allowlisted `Yusuke Watanabe <ywatanabe@scitex.ai>`, pinned through
`SAC_GIT_AUTHOR_EMAIL` (direnv `.envrc`) → `GIT_AUTHOR_EMAIL` (the
apptainer alias step in `runtimes/_apptainer_inner_argv.py`). That pin
can **silently** fail — direnv never fired / the var is empty, so
`GIT_AUTHOR_EMAIL` is unset and git synthesizes `user@hostname`; or a
prompt-level `git config user.email` override moves the author away.
Nothing caught it until CLAssistant, after CI.

> This exact silent failure was reproduced live while building this
> hook: a fresh `scitex-agent-container` worktree resolved its author to
> `proj-scitex-agent-container <agent@scitex-agent-container>`
> (`GIT_AUTHOR_EMAIL` unset, a non-allowlisted local `user.email`). The
> guard blocks that at commit time.

## What

- `enforce_commit_author_allowlist.sh` — PreToolUse Bash hook. On
  `git commit` / `git push` it resolves the **effective** author email
  and **BLOCKS (exit 2)** when it is not allowlisted, with a message
  that (a) tells the agent to fix identity BEFORE pushing, and (b)
  explicitly distinguishes "wrong local identity" from "the CLAssistant
  bot itself errored". Read-only git (status/log/add), non-git,
  non-Bash, and not-in-a-repo all pass through untouched.
- `settings.local.json.fragment.json` — the PreToolUse wiring snippet to
  merge into the baseline `settings.json` (see below).

### Allowlist (case-insensitive email match)

| kind          | value                                     | maps to           |
| ------------- | ----------------------------------------- | ----------------- |
| built-in exact | `agent@scitex.ai`                        | GitHub `ywatanabe1989` (agent-authored) |
| built-in exact | `ywatanabe@scitex.ai`                    | GitHub `ywatanabe1989` (operator's own) |
| built-in glob  | `*[bot]@users.noreply.github.com`        | `bot*` authors    |
| env extension  | `CC_CLA_ALLOWED_EMAILS="a@x,b@y"`         | LLEmacs / others  |

`cla.yml` allowlists **GitHub accounts** (`bot*,ywatanabe1989`), and
CLAssistant reaches an account from a commit by its author email. Both
addresses above are verified emails on `ywatanabe1989`, so both pass; they
differ only in who `git log` credits. Since 2026-08-12 (operator-approved)
agent-authored commits use `agent@scitex.ai`, so agent work is
distinguishable from the operator's own without either failing the gate.

The default stays intentionally tight at those two: ANY other author
(`agent@<host>`, a synthesized `user@hostname`, a stray override) maps to
no allowlisted account and is the bug the hook exists to catch. For a rare
non-default-but-allowlisted identity, extend via `CC_CLA_ALLOWED_EMAILS`
rather than loosening the default. Operator-supervised bypass:
`CC_ALLOW_CLA_AUTHOR=1` or an inline `# hook-bypass: cla-author` marker.

## How to deploy fleet-wide

The hook only FIRES once it is in the materialized baseline. The package
copy here is the source of truth; propagate it exactly like
`claude_worktree_hooks`:

1. Copy the script to the shared baseline pre-tool-use dir:

       <agents_dir>/_shared/to_home/.claude/hooks/pre-tool-use/enforce_commit_author_allowlist.sh

   (In this fleet that is
   `~/.dotfiles/src/.scitex/agent-container/agents/_shared/to_home/.claude/hooks/pre-tool-use/`.)
   `runtimes/_to_home.py` materializes that tree into every agent's
   `$HOME/.claude/hooks/pre-tool-use/` on each start — no SIF rebuild.

2. Merge the `PreToolUse` `Bash`-matcher entry from
   `settings.local.json.fragment.json` into the baseline
   `<agents_dir>/_shared/to_home/.claude/settings.json` `hooks` block
   (append it to the existing `Bash` matcher's `hooks` list, next to
   `deny_commit_push_on_main_develop.sh`).

3. Restart agents (or wait for the next natural restart). Claude Code
   re-reads `~/.claude/settings.json` on each session boot.

## Verification

- `bash enforce_commit_author_allowlist.sh --self-test` — drives the
  hook against real ephemeral git repos (allowlisted vs non-allowlisted
  authors, inline `--author` / `GIT_AUTHOR_EMAIL=` / `-c user.email=`
  overrides, push-range author scan, env extension, both bypasses,
  read-only/non-git/non-Bash pass-through). Exit 0 iff all pass.
- Regression suite:
  `tests/scitex_agent_container/_baseline_assets/git_identity_hooks/`
  drives the same scenarios from pytest (no mocks, real repos).

## Provisioning side (defense in depth)

This hook is the fail-loud backstop. The upstream pin is TWO layers, both
scoped to the agent (an agent's `$HOME` is `/home/agent`, inside its own
apptainer overlay, so neither can reach the operator's `~/.gitconfig`):

1. **The per-agent spec** — `<agents_dir>/<name>/spec.yaml` carries
   `apptainer.raw_args: ["--env", "GIT_AUTHOR_EMAIL=agent@scitex.ai", ...]`
   plus the agent's own id as `GIT_AUTHOR_NAME`. Env beats git config, so
   this is what actually decides authorship.
2. **The shared to_home baseline** — `<agents_dir>/_shared/to_home/.gitconfig`,
   materialized into every agent's `$HOME/.gitconfig` on each start by
   `runtimes/_to_home.py`. The fallback for when layer 1 does not land; its
   job is to make that failure land on an allowlisted identity rather than a
   synthesized `user@hostname`.

If a host is found where the author is wrong, read layer 1 first — it wins.

> **Not direnv.** This section used to name `~/proj/.envrc` exporting
> `SAC_GIT_AUTHOR_EMAIL` as the pin. Measured on compute-04 2026-08-12:
> that file does not exist and the variable is empty, so the
> `_GIT_ENV_ALIAS_STEPS` mirror in `runtimes/_apptainer_inner_argv.py`
> contributes nothing on this fleet. The mirror is values-agnostic and
> still works if a host ever sets `SAC_GIT_*` — but documentation asserting
> a mechanism that contributes nothing sends the next reader to fix the
> wrong layer, so it is recorded here as inert rather than as the source.
