---
description: |
  [TOPIC] Observability contract
  [DETAILS] How `sac agents status <name> --json` merges registry + agent_meta + event_log into a single best-effort blob that downstream fleet hubs (any consumer) read without direct coupling — every field has a defaul....
tags: [scitex-agent-container-observability, observability]
---

# Observability contract

`sac agents status <name> --json` produces a single JSON blob by merging three
sources:

1. **Registry entry** — what was recorded at `sac agents start` time (name,
   pid, session, runtime, host, started_at).
2. **`agent_meta.collect_rich(name)`** — live process/session state
   (pane_state, screen_idle_seconds, mcp_status, model, account, …).
3. **`event_log.summarize(name)`** — recent lifecycle/health events
   (restarts, last health-fail, last quota-watch tick, …).

## Best-effort guarantee

Every field is best-effort. If the underlying probe fails (multiplexer
gone, log file missing, network unreachable), the field falls back to
its type's empty value (`""`, `0`, `[]`, `{}`) rather than raising.
This means consumers can rely on the **shape** of the output — they
just need to treat empty values as "not observed."

## Downstream coupling

Downstream orchestrators (any fleet hub, dashboards) consume this JSON
and **only** this JSON. They do not import
`scitex_agent_container` Python objects, do not read the registry
directly, and do not parse event-log files. The status JSON is the
contract.

This means:

- Adding a field is a non-breaking change (consumers ignore unknown keys).
- Renaming a field is a breaking change — bump the package minor and
  document in CHANGELOG.
- Removing a field is a breaking change.

## tmp-pressure field (`sdk_session.heartbeat.tmp_used_pct`)

For `runtime: apptainer` (claude-session) agents the per-beat heartbeat
carries `tmp_used_pct` — the fill percentage of the container's `/tmp`.
Surfaces as `sdk_session.heartbeat.tmp_used_pct` in `sac agents status
--json` (the status surface echoes the heartbeat dict verbatim).

Why it exists: inside the container `/tmp` is the RAM-backed tmpfs
(apptainer `--containall` default, unbounded by sac). A heavy
`run_in_background` Bash session writes per-command + task-output files
there; once it fills, every shell command that needs a temp file fails
with exit 1 + empty stdout — the silent "Class B" bash wedge (2026-05-22
diagnosis §3). Watching `tmp_used_pct` climb toward 100 turns that
silent failure into an observable precursor.

Best-effort: the field is **absent** (not `0`) when the probe fails
(e.g. read on the host, where there is no container `/tmp` tmpfs). Absent
≠ 0% — a reader distinguishes "not probed" from "empty tmpfs".

### Deferred: bounding the tmpfs (not in this change)

The companion mitigation — capping `/tmp` so exhaustion surfaces as a
loud `No space left on device` rather than a silent exit-1 — was
**deliberately deferred**. It requires threading a
`--mount type=tmpfs,…,size=…` (or sized `--writable-tmpfs`) into the
apptainer raw_args, which touches the iso-flags / relaxed-vs-hardened
launch path and risks regressing the live `--containall` behaviour.
Instrumentation (this field) is the cheap, non-invasive first half; the
bounding is tracked separately so it can be reviewed on its own.

## See also

- README "Rich Status" table — full field list with types and meanings.
- [10_cli.md](10_cli.md) — `sac agents status` invocation and flags.
