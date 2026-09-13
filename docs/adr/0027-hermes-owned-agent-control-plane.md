# ADR-0027: Hermes owns the agent control plane

Status: Accepted for staged migration (2026-09-10)

## Context

SAC accumulated harness-level behavior while adapting interactive Claude Code
and Codex processes to fleet operation. The Scholar Codex/Qwen experiment made
the overlap visible: SAC had to infer terminal readiness, persist messages,
retry pane injection, and translate a busy terminal into queue semantics.

Hermes 0.21.1 already exposes persistent sessions and an authenticated API with
durable idempotent runs, events, steering, and stopping. An isolated Qwen pilot
on `scitex-compute-03` observed all of the following:

- a shell-and-file task completed in 15.4 seconds;
- a new process resumed the persisted session and recalled the probe token;
- steering was accepted during a 30-second shell call and changed the verified
  output from `INITIAL\n` to `STEERED\n`;
- stopping a run during a 60-second shell call produced `cancelled`, and the
  post-sleep file was absent;
- after a validated `SIGKILL`, gateway restart detected the unclean prior life,
  persisted the in-flight run as `interrupted`, replayed the same terminal
  receipt for the original idempotency key, and did not create the post-sleep
  file;
- the initial host-side relative file write landed under home, but an
  Apptainer probe with an absolute generated `terminal.cwd` made both the file
  tool and shell resolve to scratch, verified the exact bytes, and created no
  home copy.

These are observations from a pinned upstream checkout at commit
`a74e76632cce62ad6948cf7e4e6b27629d66d147`, not assumptions based only on a
feature list.

The first production Scholar incarnation then established a second set of
observations on 2026-09-10:

- the image-installed Hermes 0.21.1 gateway, Qwen engine, turn bridge, and
  inbox bridge were started by `sac agents start scitex-scholar`;
- the stable session `sac:scitex-scholar` retained
  `SCHOLAR_HERMES_RESUME_0910` across a real SAC stop/start and returned it
  exactly on the next turn;
- the marker-writing turn completed in 394.7 seconds with 160,100 input
  tokens, while post-restart recall completed in 3.8 seconds with 97,310 input
  tokens;
- a real SAC bus message had one live subscriber, was admitted under its
  message idempotency key, and completed with the requested
  `SAC_EXPLICIT_ACK_GATE_0910` output;
- an explicit-ack probe remained in PostgreSQL after its SSE frame was read
  and left the undelivered set only after `POST /agents/<name>/inbox/ack`.

The same run exposed SAC defects rather than model defects. A reported
successful stop left stale liveness evidence that made the immediate start
skip; `start --force` produced a new verified process. The listener's
`/agents` row also retained the exited launch-wrapper PID. Hermes received the
complete Claude commands and hooks tree twice even though it does not consume
those assets. These observations became acceptance requirements for the
thinning work, not inferred architectural concerns.

After gating Claude home assets by harness, a production Scholar restart
reported zero host-command copies, zero host deep-merge links, and zero
`.claude` deployment lines; SAC still verified the new Hermes process and its
turn and inbox bridges. Neutral `to_home` files such as `.mcp.json`, `.env`,
and `.gitconfig` remain available to Hermes. A second post-thinning restart
again recalled `SCHOLAR_HERMES_RESUME_0910` exactly (109,788 input tokens, 57
output tokens, 231.7 seconds), so the removed Claude assets were not carrying
Hermes session continuity.

The lifecycle corrections were exercised on the production Scholar agent on
2026-09-10. `sac agents stop` did not return until Apptainer PID 1846511 was
absent and wrote a terminal heartbeat with `state=stopped` and
`writer=sac-hermes-stop`. An immediate ordinary `sac agents start`, without
`--force` or an inserted delay, classified the prior incarnation as dead and
started PID 1862324. The registry file, status evidence, and listener
`/agents` row all reported that same live Apptainer PID; the listener also
reported one reachable inbox subscriber and no fault.

That restart exposed and then verified one additional adapter correction.
Hermes legitimately answers turn admission with HTTP 202 plus a run receipt,
not a completed `text` field. The neutral peer client now accepts either a
completed text response or a delivered receipt containing a non-empty
`run_id`. Production `sac agents send` printed
`accepted run run_42849dcaa74e499e8dc942e01f200aef (status=started)`, and the
Hermes run then completed with the exact requested
`SAC_POST_RESTART_0910_B` output. It reported 130,950 input tokens, further
observing that the stable session context survived the lifecycle correction.

The browser adapter follows the same thin-boundary decision. Fleet and agent
detail pages read the authenticated SAC listener rather than reconstructing
runtime state in Django. Start, Stop, and Restart are CSRF-protected browser
POSTs restricted to an explicit operator allowlist, then delegated to the
listener's existing lifecycle endpoints. The dashboard stores no lifecycle
state of its own. Ambiguous observations are split into never-started,
stopped, registry-only, status-endpoint-unavailable, communication-error, and
residual unknown states so absence is not presented as failure without
evidence.

The fleet reconciler initially remained terminal-specific. Its five-minute
scheduled pass treated the absence of a tmux session as proof that the
headless Hermes runtime was dead, sent SIGTERM, and restarted Scholar about
every 30 minutes. The production log recorded shutdowns at 18:21, 18:51,
19:21, 19:51, 20:21, and 20:51 UTC. The reconciler now asks the selected
runtime for liveness when `runtime` is not `tui`, while retaining the batched
tmux observation for TUI agents. A failed runtime probe is `UNKNOWN`, never a
death observation. The vanished-tmux-server fleet-blackout gate likewise
applies only to TUI restart candidates; it cannot withhold independent
headless-runtime recovery. After deployment, the scheduled 21:40 UTC pass left the
same Scholar Apptainer PID 1880540 alive and no later SIGTERM appeared. The
pass as a whole still exited 2 because the unrelated `tmp-nas` spec was
invalid; this did not weaken the per-agent Scholar observation. On 2026-09-10
that legacy spec was replaced with a comment-free, fully explicit v3 spec
using the canonical `available_harnesses` and `available_engines` surfaces.
`validate_config` then returned no errors, and a production-host
`sac agents reconcile --json` exited 0 while classifying `tmp-nas` as
`SKIPPED/pinned-elsewhere`. This removed the unrelated global reconcile fault
without starting the probe or altering the live Scholar incarnation.

`sac agents status scitex-scholar --json` now exposes the neutral runtime
surface used by operators and future GUIs: harness, engine, live registry PID,
context limit, reasoning effort, stable session identity, control-plane kind
and port, and the non-secret names/transports of configured MCP servers. The
production result reported `hermes`, `qwen38-27b`, PID 1880540, a 1,048,576
token context limit, low reasoning effort, session `sac:scitex-scholar`, the
Hermes gateway on port 19009, and the SAC and Cards stdio MCP servers.

## Decision

SAC will become a thin deployment and fleet-routing layer around Hermes for the
first Qwen application-agent migration.

SAC retains ownership of:

- declarative agent specifications and validation;
- harness and inference-engine selection;
- image provenance, Apptainer isolation, volumes, workdir, and host placement;
- secret injection and process supervision;
- stable public agent routing;
- neutral `agent_id`, `spec_id`, `session_id`, and `incarnation_id` records.

Hermes owns:

- the agent loop, tool execution, and context management;
- native session persistence and resume;
- busy-input queueing, steering, interruption, and run cancellation;
- messaging gateways, memory, skills, goals, subagents, Kanban, cron, and
  heartbeat when enabled by the generated configuration.

SAC still owns the neutral permission and resource envelope for that feature.
`spec.lineage.may_spawn` is carried into the launch plan; a false value becomes
Hermes `agent.disabled_toolsets: [delegation]`, which removes `delegate_task`
after the `hermes-cli` bundle is expanded. `spec.delegation` carries the
provider-independent child-width and Git-worktree request. The generated
profile states all of these values, fixes nested delegation off at depth one,
and defaults to two children instead of inheriting Hermes' upstream width of
ten. Engine names do not imply policy: an external DeepSeek agent can author a
lower cap and a local Qwen agent a measured higher cap without coupling either
engine to Hermes.

Worktree isolation is conditional upstream behavior, not an Apptainer promise:
it operates for Git workspaces on Hermes' local terminal backend. Cards remains
the ownership ledger for collaborative work, and concurrent editing children
must receive distinct Cards and branches/worktrees. They share the parent's
container, mounted filesystems, network, and service credentials.

The production boundary is Hermes' authenticated gateway API. SAC maps its
neutral `message_id` to `Idempotency-Key`, its stable session identity to the
Hermes session key/id, and Hermes terminal run status to a neutral completion
receipt. SAC does not duplicate the Hermes queue or import private Hermes
Python modules.

Apptainer is the tool-permission boundary. A SAC-managed Hermes process runs
with harness approvals disabled whether or not autonomous turn driving is
enabled. `autonomous.enabled` controls continued work; it does not control
whether a tool call needs human confirmation. This was verified on
`scitex-compute-03` on 2026-09-10: the generated profiles for Hub, App,
Figrecipe, Cards, and Scholar all reported `approvals.mode: off`, while their
repository and credential visibility remained limited by their Apptainer
binds and injected environment.

The same fleet run exposed two independent message-inbox defects. Compute-03
was running `scitex-dev` 0.56.3, whose store-open path issued owner-only DDL
even for an existing schema; upgrading the host environment to 0.59.0 allowed
the restricted `ywatanabe__sac-listen` service role to open the inbox with DML
rights on only its four tables. SAC then still left every message at zero
attempts because it used `Row.seq` (the replication oplog coordinate) as the
optimistic record revision. The observed failing pair was oplog sequence 487
versus record revision 1. SAC now obtains `Store.revision(row.key)` before each
claim. After deployment, App advanced all four queued turns to `delivered`,
Figrecipe advanced all three, and busy UI/Writer turns remained `accepted`
instead of being lost.

The authored spec remains the single configuration source. SAC compiles a
pinned Hermes configuration into the incarnation's derived runtime directory.
That artifact and the exact Hermes version/commit are included in the launch
receipt; they are not edited by operators and are not shared state. Runtime
state remains in the configured state stores.

The neutral PostgreSQL inbox and TUI bridge are transport adapters. The Hermes adapter uses the inbox only as a durable transport: it
requests `ack=explicit` and acknowledges a row only after Hermes accepts the
turn through the neutral TUI endpoint. SAC starts one loopback-only Hermes
WebSocket/JSON-RPC gateway as the session owner and attaches the official Ink
TUI to it. The adapter observes `session.active_list`: active sessions receive
the message through the intent-level `session.steer` RPC, while idle sessions
receive an ordinary `prompt.submit`. This is an explicit transport contract,
not an inference from the TUI's `busy_input_mode` preference. An A2A caller may
request `delivery_mode: queue`, which maps to `prompt.submit` with Hermes'
explicit `queued: true`; every omitted mode defaults to `steer`. A rejected
steer and any semantic downgrade to the next-turn queue fail the exchange
loudly. It never types messages into tmux. A bridge failure before acceptance
therefore leaves the row
replayable. The host-side adapter receives the listener bearer only through
its environment, and its stop path signals a recorded PID only after Linux
process identity proves the exact module, agent name, and authored spec path.
This is delivery plumbing, not a second queue or scheduler. Message delivery
uses Hermes' native steering behavior. Explicit `/v1/control` events are
limited to Enter and Escape for human modal control; they use the attached
tmux PTY and are never used for message delivery.

Hermes 0.21.1 does not implement Claude Code's
`notifications/claude/channel` terminal-rendering extension, and its public
gateway has no server-to-TUI notification method. A successful MCP stdio write
therefore cannot prove that a human or agent saw a Cards notification. For a
Hermes TUI, SAC instead reads Cards through its public non-destructive
`poll_notifications(..., ack=False)` API, submits a sender-attributed
`<channel>` turn through Hermes' native `prompt.submit` RPC. The delivery
boundary borrows HTTP through `scitex_dev.status`: the turn bridge first
persists a `202 Accepted` plus exchange id in the shared `status_exchanges`
ledger and returns immediately, then a serialized worker records the separately
observed final `200`. A notification whose producer supplied an exchange keeps
that identity. A non-final `http/102` remains the same attempt and is polled
again. A final failure is immutable: Cards must persist a new exchange for the
next attempt while retaining the same delivery and operation identity. The
Cards poller confirms the Cards id only after polling to a final `http/200` and
proving the unique delivery marker in Hermes. The bounded proof reads live
inflight/queue state with `session.activate(omit_messages=True)` and uses
Hermes' authenticated, size-capped session search for older accepted user
input; it never reconstructs the full transcript. ADR-0031 records the
canonical exchange lifecycle and the measured reason for that projection.
The poll selects Cards' `unconfirmed` ids rather than only unseen ids: the
legacy Claude channel can stamp a transport push as seen even though Hermes
ignored its unsupported notification method. A busy turn is accepted through
Hermes' native `steer` mode. Existing staged human text or a modal is never
overwritten; a failure carries an actionable native `StatusCode.message` and
the Cards row remains unconfirmed for retry. Diagnostic questions use
`scitex_dev.status.Check`'s three-valued wire shape rather than a local delivery
taxonomy. The same positive-visibility gate
backs SAC inbox explicit ACK, so neither rail equates sent keystrokes with
delivery.
They can be removed only after the image-installed Hermes path demonstrates
durable replay across process restart, MCP/tool availability, model provenance,
and one representative Scholar task. Absolute-workdir isolation has passed
using the current SAC image with the pinned Hermes scratch venv bound into it.

An interrupted Hermes run is a terminal receipt, not an instruction to execute
the same message again. SAC may apply its neutral retry policy only by issuing
a new message id; it must never defeat Hermes idempotency by silently changing
the payload under an existing key.

## Consequences

SAC becomes smaller by delegating mature harness behavior rather than
maintaining terminal-specific emulation. The interface remains replaceable:
stored identities and message semantics do not contain Hermes, Codex, Claude
Code, TUI, or SDK protocol event names.

Home materialization is harness-aware. Claude commands, hooks, skills,
settings, and host deep-merge are compatibility assets for Claude-family
harnesses and are not copied into a Hermes incarnation. Neutral files retain
the normal `to_home` cascade.

Hermes becomes a pinned optional image dependency and an operational component
that must be upgraded through the acceptance suite. ACP remains available for
compatible editors, while the gateway API is the fleet-control boundary.

The migration is deliberately staged. Scholar has passed the production
session-resume, SAC-bus admission, explicit-ack, Cards MCP, and representative
repository-task gates. This authorizes removing Claude-only materialization
from the Hermes launch path. It does not yet authorize deletion of the working
Codex rollback path or a fleet-wide default change.
