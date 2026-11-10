# Board-directed Stop-hook continuation

`never-stop-when-task-remains` is the Stop-hook adapter emitted by
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
sac never-stop-when-task-remains --help
sac never-stop-when-task-remains --agent example-agent < stop-event.json
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
instruction. This clarification changes no emitted hook command, provider
selection, deadline, identity rule, decision, state transition or retry limit.
