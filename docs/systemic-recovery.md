# Systemic recovery

Run periodic SAC jobs through one `scitex-dev ecosystem run` supervisor per
host. `scitex-dev ecosystem up --yes` installs that supported supervisor and
retires the managed cron block. It does not restart an already-running
supervisor or retire all historic leaf timers. After checking installed job
discovery and fresh periodic execution receipts, stop/disable duplicate
`scitex-agent-container-{fleet-reconcile,restart-login-expired-agents,
resume-rate-limited-agents}.timer` units. Keep authored unit configuration in
dotfiles and package-owned mechanisms in SAC/Dev, with no external helper.

The five-minute fleet reconciler handles agents with `restart.policy` set to
`always` or `on-failure` whose local session is gone. It respects deliberate
stops, host placement, a thirty-minute debounce, two attempts per rolling
hour, a per-pass cap, and its fleet-blackout refusal. Its ordinary restart
path verifies a usable Hermes successor and workspace ownership before
stopping; conversation, image, and environment reuse stay on that path.

The auth-banner job now declares `--check`. Even old jobs carrying `--apply`
cannot bypass positional/liveness report-only admission. A frozen historical
auth banner can remain on a healthy idle session. Unproven candidates produce
visible `UNOBSERVED` reports and exit 2, without restart attempts, confirmed
auth-failure events, or restart-history writes. Successor credential health
proves that a replacement could run, not that the current session has stalled.

Rate-limit resumption waits for a published reset and verified delivery; it
does not restart during a standing quota wall or select a different model by
default. The existing matcher recognizes Claude quota banners. Hermes native
credential rotation remains Hermes-owned. SAC's Hermes stale-latch observer
recognizes one exact native terminal error and can rebind the same session
only when a gateway health response explicitly proves admission capacity.
That capacity contract is not an external-provider auth or quota probe.

Hermes exposes read-only active-session status, message count, last-active
time, and sequenced terminal events. These are useful recovery inputs but do
not by themselves prove progress on assigned work. A future native recovery
rule needs a durable open, unblocked assignment; unchanged turn/tool evidence
over a bounded window; no user-input/approval pause; and a verified successor.
Unknown telemetry and healthy idle must remain report-only. The existing
`sac listen` card-liveness loop emits alarms for stale open, unblocked cards
every two minutes (fifteen-minute stale threshold); it does not restart them.

Use `~/.scitex/dev/runtime/periodic-executions.jsonl` for execution receipts,
including exit codes and overlapping-run skips. An enabled timer alone does
not prove that recovery ran or that its observations were conclusive.
