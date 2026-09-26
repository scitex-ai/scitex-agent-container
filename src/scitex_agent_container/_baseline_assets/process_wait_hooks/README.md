# Process wait hooks

`deny_self_matching_pgrep_wait.sh` is a Claude Code `PreToolUse` guard for a
specific non-terminating background-waiter failure:

```bash
until ! pgrep -f "worker-name"; do sleep 5; done
```

The submitted text appears in the full command line of the surrounding
`bash -c` process. The regex `worker-name` therefore keeps matching that shell
after the real worker is gone.

The guard is declared by `scitex_agent_container._claude_hooks_plugin` and
materialized centrally by `scitex-dev`. It refuses only `while`/`until` loops
whose `pgrep -f` (or `pgrep --full`) regex can be shown to match the submitted
command text. Standalone probes, exact-name/PID probes, anchored patterns that
do not match the waiter, and the conventional `[w]orker-name` self-exclusion
remain allowed.

Prefer retaining `$!` and using `wait "$pid"`. When the process was launched
elsewhere, use a self-excluding pattern and inspect `pgrep -af` once before
polling.
