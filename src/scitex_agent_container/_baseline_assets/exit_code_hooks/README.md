# Exit-code hooks

`deny_swallowed_exit_code.sh` is a Claude Code `PreToolUse` guard that makes
exit-code verification mandatory for `Bash` commands (operator order CCT 4552:
terminal calls must surface real exit codes, pipe usage forces pipefail, no
silent swallowing).

It refuses two provable failure-hiding shapes:

1. **A pipeline without `set -o pipefail`** (or an explicit
   `${PIPESTATUS[...]}` check). A pipeline's exit code is its *last*
   command's, so `pytest -q | tail -5` succeeds whenever `tail` succeeds --
   even when every test failed.
2. **A provable swallow**: `|| true`, `|| :`, `|| exit 0`
   (`/bin/true` included), or a trailing `; true` / `; exit 0`. These convert
   failure into success unconditionally, so CI verdicts, retry loops, and
   delivery gates key on the wrong signal.

The detector is token-based (shell `shlex` split with `punctuation_chars`),
not regex-based, so quoted text cannot trigger it: `grep -E "a|b" file` and
`echo "x || true"` are data, not shell, and stay allowed. `case a|b)`
alternations are exempt via case-depth tracking. Deliberately still allowed:
`cmd || <real handler>` (`|| echo`, `|| exit 1`), `cmd && true` (does not
swallow -- `false && true` still exits 1), `! cmd` negation, and bare
`2>/dev/null` (preserves the exit code). A tokenizer failure fails OPEN.

The guard is declared by `scitex_agent_container._claude_hooks_plugin` and
materialized centrally by `scitex-dev`. Overrides (rare, know why):
`SAC_ALLOW_SWALLOWED_EXIT_CODE=1` or an inline
`# hook-bypass: swallowed-exit-code` marker.

Verify at any time: `bash deny_swallowed_exit_code.sh --self-test`.
