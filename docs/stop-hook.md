# Board-directed Stop-hook continuation

`dev hooks decide-stop` is the Stop-hook adapter emitted by
[`runtimes/settings_json.py`](../src/scitex_agent_container/runtimes/settings_json.py).
The configured board provider reports whether work calls for continuation.
SAC resolves agent identity, invokes that provider,
forwards its decision and applies fail-open handling and the bounded loop guard.
Preferred provider-authored hook JSON is forwarded unchanged. The existing
legacy path also accepts exit 2 with a parsed `runnable` verdict and composes
a bounded reason from structured `items` fields (`card_id`, `reason`,
`next_action`). Raw provider stderr never becomes a continuation instruction.
An exit status of 2 alone cannot distinguish pending work from a CLI usage
error and is treated as unknown, not a positive block.

For a harness event captured in `stop-event.json`:

```bash
sac dev hooks decide-stop --help
sac dev hooks decide-stop --agent example-agent < stop-event.json
```

Replace the example agent with the intended agent identity. The command reads
the event JSON from stdin through EOF. Running it invokes the board provider
and can update hook state; this is not a passive health check. The example
shows the existing protocol, not a request to install another hook or poller.

In the implemented protocol, status 0 with no stdout allows the stop. A JSON
`decision: block` with a reason requests continuation; it does not prove a task
was claimed, started or completed. An operator-queue `systemMessage` can be
present while still allowing the stop. Human diagnostics stay on stderr.
Unreadable, missing, crashing or timed-out providers fail open with an alarm;
repeated unchanged block reasons eventually trip the existing loop guard and
allow the stop. These are distinct from an observed clear board.

The CLI and detector tests exercise real owned provider executables, stdin,
loop-state files, protocol forwarding, unknown results and operator-queue
reports. They do not establish that a live agent followed a continuation
instruction. The shared backend retains provider selection, deadlines,
identity rules, decisions, state transitions and retry limits. The settings
command migration is described below.

## Canonical runtime entry point

New generated settings use `scitex-agent-container dev hooks decide-stop`.
It shares the existing callback and options with the retained
`never-stop-when-task-remains` protocol entry point: stdin payload consumption,
provider interpretation, fail-open alarms, bounded continuation counter and
stdout JSON remain unchanged. A block requests continuation; it does not prove
that a task was claimed or executed. The command invokes the configured provider
and may update hook state; it is not a passive inspection command.

The old entry point is a hidden compatibility alias until 0.40.0. It forwards
through the real `dev hooks decide-stop` path, preserving stdin, options and
protocol stdout. Its deprecation warning goes to stderr. It does not use an
exit-2 rename redirect, which could itself block a deployed Stop hook.

Settings composition prunes both historical Stop names and the new spelling
before reinjection, preserving unrelated hooks. Existing `exclude_hooks`
substring opt-outs for historical Stop names also match the canonical spelling.
This source migration does not rewrite deployed settings or qualify a vendor
harness, external provider, live board, wheel or fleet rollout. Upgrade executable
and generated settings together before rollout.

Source: `cli_pkg/hooks_group.py`, `never_stop_when_task_remains_cmds.py`,
`runtimes/settings_json.py` and `_settings_hooks.py`. Controls live in their
matching CLI/settings tests and the existing provider/loop-guard tests.
