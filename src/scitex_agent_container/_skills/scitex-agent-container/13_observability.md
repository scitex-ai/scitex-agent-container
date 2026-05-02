---
name: observability-contract
description: How `sac status <name> --json` merges registry + agent_meta + event_log into a single best-effort blob that downstream orchestrators (e.g. scitex-orochi) consume without direct coupling — every field has a default fallback so partial failures do not raise.
tags: [scitex-agent-container, observability]
---

# Observability contract

`sac status <name> --json` produces a single JSON blob by merging three
sources:

1. **Registry entry** — what was recorded at `sac start` time (name,
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

Downstream orchestrators (`scitex-orochi`, fleet dashboards) consume
this JSON and **only** this JSON. They do not import
`scitex_agent_container` Python objects, do not read the registry
directly, and do not parse event-log files. The status JSON is the
contract.

This means:

- Adding a field is a non-breaking change (consumers ignore unknown keys).
- Renaming a field is a breaking change — bump the package minor and
  document in CHANGELOG.
- Removing a field is a breaking change.

## See also

- README "Rich Status" table — full field list with types and meanings.
- [10_cli.md](10_cli.md) — `sac status` invocation and flags.
