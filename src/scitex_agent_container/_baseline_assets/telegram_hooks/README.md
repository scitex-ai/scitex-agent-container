# Telegram format-hook scripts (OP-PRIO-FMT, 2026-06-09)

Canonical, version-controlled implementations of the 5 telegram-message
format enforcers the operator priortised on 2026-06-09. Ships alongside
`claude_worktree_hooks/` under
`scitex_agent_container/_baseline_assets/telegram_hooks/`, but is
**not** auto-deployed — the operator copies (or symlinks) the scripts
into their dotfile baseline tree so the runtime's `to_home`
materializer drops them into every agent's `$HOME/.claude/hooks/
pre-tool-use/` on every start.

The matcher is **the FQ MCP tool name** (`mcp__claude-code-telegrammer__reply`).
A literal `"telegram"` matcher does NOT fire on MCP tool names — that
was the OP-PRIO-2 bug fixed the same day; do not regress it.

## Scripts

| Script | Rule | Behaviour | Escape env |
|---|---|---|---|
| `enforce_telegram_numbering.sh` | 1. Lettered options must be numbered (`1a/1b` or `1./2.`) | BLOCK | `CC_ALLOW_LETTERED_OPTIONS=1` |
| `enforce_telegram_no_bare_issue.sh` | 2. Every `#NNN` **anywhere** in the message needs a parenthetical description — `#970（説明）` / `#970 (description)` | BLOCK | `CC_ALLOW_BARE_ISSUE=1` |
| `enforce_telegram_use_lists.sh` | 3. 3+ enumerated items must be a list, not run-on prose | BLOCK | `CC_ALLOW_PROSE_ENUM=1` |
| `enforce_telegram_no_filler.sh` | 4. No filler / hedging words (`basically`, `actually`, `一旦`, `とりあえず`, ...) | BLOCK | `CC_ALLOW_FILLER=1` |
| `encourage_telegram_terse_style.sh` | 5. Long sentences should end in a terse closer (`します/しました/done/...`) | REMINDER (stderr nudge, rc=0) | `CC_ALLOW_NON_TERSE=1` |

### Rule 2 was tightened on 2026-08-11

As shipped on 2026-06-09 the hook only refused a message that reduced
ENTIRELY to bare `#NNN` tokens, so a number inside a sentence passed
untouched — which is the common case. The operator noticed after being
sent a line containing `#970` with no description and asked why his rule
was not enforced. **A guard whose trigger condition is narrower than its
stated rule reads as enforcement while enforcing almost nothing.**

The hook now refuses any `#NNN` that is not immediately followed by a
parenthetical description, with three documented decisions: URLs are
blanked before scanning (a `…/pull/970` path segment and a `…#123`
fragment are not references); a repeated `#NNN` inherits the description
given EARLIER in the same message, left to right; and a repo name is not
a description (`scitex-dev #578` still needs one). The rationale for
each lives in the script header. Case table + mutation check:
`tests/integration/telegram_hooks/test_telegram_no_bare_issue_rule.py`.

Each script supports `--self-test` and emits a `pass=N fail=M` summary
plus rc=0 (all pass) / rc=1 (any fail). The companion pytest at
`tests/scitex_agent_container/_baseline_assets/test_telegram_hooks.py`
runs every script's self-test in CI so a regression to either the
script logic or the test contract surfaces on PR.

## Deployment (operator)

The operator's dotfile baseline tree (`_shared/to_home/` — see
`runtimes/_to_home.py`, OP-PRIO-3 rename) is the materialization
source. Two acceptable layouts:

1. **Copy** the scripts into the dotfile:

       cp <repo>/src/scitex_agent_container/_baseline_assets/telegram_hooks/*.sh \
          ~/.dotfiles/src/.scitex/agent-container/agents/_shared/to_home/.claude/hooks/pre-tool-use/

2. **Symlink** the dotfile directory to the canonical version:

       ln -s <repo>/src/scitex_agent_container/_baseline_assets/telegram_hooks/*.sh \
          ~/.dotfiles/src/.scitex/agent-container/agents/_shared/to_home/.claude/hooks/pre-tool-use/

   Symlinks under `to_home/` are dereference-copied at materialize time
   (see `_symlink_resolve.deref_copy_symlink`), so the agent's
   `$HOME/.claude/hooks/...` ends up holding real script content.

After the copy/symlink, merge the wiring into
`_shared/to_home/.claude/settings.local.json` — the
`settings.local.json.fragment.json` sibling shows the exact JSON to
splice into the top-level `hooks.PreToolUse` array.

Restart agents (or wait for next natural restart). NO SIF REBUILD —
`_to_home.deploy_to_home` runs on every start and lays the new
scripts + settings before Claude Code reads its config.

## Live-fire verify

After deployment, send a deliberately violating Telegram message from
ANY restarted agent (the operator's preferred verification path):

    1a) packed numbered  -> telegram_line_spacing.sh blocks
    1b) bare #162        -> enforce_telegram_no_bare_issue.sh blocks
    1c) "Basically X"    -> enforce_telegram_no_filler.sh blocks

If any rule fires NUDGE (not BLOCK), the matcher fix did NOT land —
re-check `settings.local.json` for the `mcp__claude-code-telegrammer__reply`
FQ matcher (OP-PRIO-2).

## Forbidden-words YAML (lead directive 2026-06-09)

A separate hook (`forbidden_words.sh`, canonical schema + loader owned
by scitex-dev) is already wired into the operator's dotfile baseline
under the same matcher block. It reads a YAML config at:

* `~/.scitex/dev/config/forbidden-words.yaml` (global)
* `<cwd>/.scitex/dev/config/forbidden-words.yaml` (project-specific)

The lead added three **distancing/disowning** phrases to ban
fleet-wide on 2026-06-09: `既存の問題`, `関係ない`, `無関係`. The
data lives in two version-controlled places:

1. `<repo>/.scitex/dev/config/forbidden-words.yaml` — picked up
   automatically when an agent runs with `cwd=/work` (the project-local
   layer in the loader's UNION).
2. `<repo>/src/scitex_agent_container/_baseline_assets/telegram_hooks/forbidden-words.yaml`
   — canonical deployable; operators copy or symlink it into
   `~/.scitex/dev/config/forbidden-words.yaml` so every host (and the
   lead's own session) blocks the same words.

       cp <repo>/src/scitex_agent_container/_baseline_assets/telegram_hooks/forbidden-words.yaml \
          ~/.scitex/dev/config/forbidden-words.yaml

After deployment a Telegram reply containing any of the three phrases
hits `rc=2` + a stderr nudge naming the word, the reason it is
forbidden, and the suggested replacement. The pytest at
`tests/scitex_agent_container/_baseline_assets/telegram_hooks/test_forbidden_words_yaml.py`
pins both YAML copies stay in sync on the disowning trio so a
project-local override never silently drops a phrase.
