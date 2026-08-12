# ADR-0022 — `sac listen` is not a JobSpec

Status: accepted (2026-07-05, re-affirmed 2026-08-12)

## Context

`scitex-dev` derives a systemd unit FILENAME from `JobSpec.name` verbatim:

```python
def systemd_unit_name(job: JobSpec) -> str:
    return f"{job.name}.timer" if job.kind == "timer" else f"{job.name}.service"
```

The `sac listen` that actually runs on the host is `sac-listen.service` — a
HYPHEN — hand-written 2026-07-05 14:38, `Restart=always`, with `10-venv-path`
and `20-hardening` drop-ins (and, on compute-04, a `50-secrets-envrc.conf`
carrying 28 secret paths).

## Decision

`sac listen` is **deliberately not declared** in `_jobs_plugin.provide_jobs()`,
and re-adding it as `sac.listen` would take the fleet's control plane down.

A `sac.listen` JobSpec materialises `sac.listen.service`. The two names differ
by one character and systemd treats them as unrelated units, so
`scitex-dev service ensure sac.listen` does not adopt the running supervisor —
it installs a SECOND one. Two units, both `Restart=always`, both running
`sac listen`, both binding 127.0.0.1:7878: they fight for the port forever, and
every lost round destroys the in-memory Broker, which deafens EVERY agent's
inbox at once.

## What was measured

PR #543 declared it on the premise that `sac listen` "had NO SUPERVISOR". That
premise was **false by the time it merged** — the hand-written unit was created
the SAME DAY the PR was opened, and had been supervising listen for nine days
(`NRestarts=0`). The PR was obsolete on arrival and nobody re-checked before
merging it.

The clew incident (`clew-incident-sac-host-listen-down`, 2026-07-05) that
motivated federating listen was ALREADY fixed on the day it happened, by that
hand-written `sac-listen.service` — not by a JobSpec. The fragile
`sac-listen-watch.sh` `*/2` cron it replaced is gone. Re-federating it does not
fix that incident again; it only adds a second supervisor to fight the first.

## Consequences

* If listen is ever federated, it must be named `sac-listen` (hyphen) so the
  derived unit is the one that already exists — and even then, `ensure` must be
  **shown to ADOPT** the running unit rather than overwrite its drop-ins. Do not
  re-add it without measuring that.
* The canonical-name migration
  (`sac dev migrate-job-names`, `_jobs/_migrate/`) lists `sac-listen.service`,
  `sac-listen.timer`, `sac.listen.service` and `sac.listen.timer` in
  `NEVER_TOUCH`, and `assert_never_touches_listen` runs on every plan the
  planner returns — so the guard is enforced, not documented.
* Under the ecosystem naming convention (PS-226/PS-227) a future federated
  listen would be `scitex-agent-container-listen`, which is a THIRD name again.
  That rename is only safe through the same stop → remove → install →
  verify-exactly-one ordering the migration package implements.

## Related

* ADR-0017 — credential rotation and the refresh race (the other
  single-supervisor invariant in this repo).
* `src/scitex_agent_container/_jobs/_migrate/_renames.py` — `NEVER_TOUCH`.
* `src/scitex_agent_container/systemd/sac-listen.service.template` — the tracked
  template for the hand-written unit.
