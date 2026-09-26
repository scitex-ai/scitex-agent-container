# ADR-0029 — In-place Hermes stale-provider recovery

## Status

Accepted, 2026-09-11.

## Incident

Hermes 0.21.1 keeps a consecutive stale-provider streak in the live agent
object. At the configured threshold, later turns abort before making a network
request. External gateway recovery therefore cannot reset an already-latched
session, and Hermes' native session heartbeat keeps firing turns that abort at
the local breaker.

Hermes has no external RPC in the pinned release for reading or clearing that
in-memory streak. It does provide documented session-scoped controls:
`/heartbeat pause`, `/model ... --provider ... --session`, and
`/heartbeat resume`. The model command rebuilds the live provider client and
resets the streak without creating a session or clearing conversation history.

## Decision

SAC publishes an additive, harness-neutral runtime control observation:

```json
{
  "runtime_control": {
    "turn_admission": "stale_latched",
    "detail": "provider stale circuit breaker latched after 5 attempts",
    "observed_at": 1789100000.0
  }
}
```

The optional runtime methods are `control_state()`,
`suspend_autonomous_turns()`, and `recover_turn_admission()`. Status and the
Agents GUI understand only this neutral shape. Hermes-specific detection and
actions remain in the Hermes TUI adapter.

For an autonomous Hermes TUI, SAC starts one host-side recovery observer. It
recognizes only Hermes' exact stale-breaker terminal error in the live pane.
On first observation it pauses the native heartbeat. On the next bounded tick
it performs one non-generating `GET /health` against the configured inference
provider. Recovery requires an explicitly active member with immediate
capacity (`in_flight + queued < capacity`); process health alone is not enough.
It then rebinds the same model/provider in the same session and resumes the
heartbeat.

The observer polls once every 30 seconds and its first tick is spread by a
stable per-agent SHA-256 offset across that window. There is no inner retry.
One observed error occurrence can cause one successful rebind; a later breaker
error has a distinct transcript-prefix fingerprint and may start a new bounded
cycle. These bounds prevent all fleet members probing or recovering at once.

## Identity and persistence

Recovery never calls SAC stop/start, Hermes `/new`, or any session cleanup.
It writes only `runtime-control.json` and its own PID/log files in the SAC
runtime directory. The Hermes session id, SAC incarnation id, and Hermes
`state.db` context remain unchanged.

The committed integration regression uses a behavioral fake session around
real local HTTP health and completion boundaries. It proves the adapter's
orchestration and persistence invariants, but is not production proof of the
pinned Hermes TUI path; validation in a disposable real Hermes environment
remains follow-up operational evidence.

## Relation to issue #1340

This observer recognizes an exact terminal breaker error; it does not decide
whether an ordinary prompt was submitted. Busy-marker and composer/transcript
disambiguation belongs to #1340 and is intentionally unchanged here.
