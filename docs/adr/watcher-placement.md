# Watcher execution location + startup (audit item 1) — decision

## Decision
- **Service:** GitHub Actions. **Scheduler:** `schedule: cron '*/10 * * * *'` + `workflow_dispatch`.
- **Machine:** `runs-on: ubuntu-latest` (GitHub-hosted) — never a self-hosted runner.
- **Ledger:** append-only JSONL committed to orphan branch `ci-watch-ledger`
  (agents gate DONE via `git fetch origin ci-watch-ledger` + `assert_done_allowed`).
- Draft: `ci-watch.yml` next to this file. Land it in scitex-agent-container
  `.github/workflows/` once the watcher module merges to develop.

## Why not the alternatives
- Same self-hosted runner/host as CI → 共倒れ (dies with what it watches).
  All four self-hosted runners online+busy as of 2026-10-08T09:3xZ
  (`scitex-ci-02/03/04/04-02`, `busy:true` via org runners API).
- cron on compute-04: no cron daemon (`crontab: command not found`,
  verified 2026-10-08 on scitex-compute-04). No user systemd bus either.
- `sac listen` side task: dies with the agent — fails the "agent stopped" requirement.

## Verified live 2026-10-08 (~09:30Z, from bare shell on scitex-compute-04)
- Agent stopped: poll ran as plain `/uvwork/venv-agent/bin/python`, no agent,
  listener, broker, or `_ci_watcher` process exists
  (`evidence/liveness-probe.sh` → none found).
- Target runner stopped: no `Runner.Worker`/`Runner.Listener` on compute-04,
  yet `poll_once` against the real API recorded run 37741732373 → `failure`
  (`evidence/ci-watch.jsonl`, recorded_at 2026-10-08T09:30:54Z).
- Stopped watcher reads stalled, never healthy: fresh `success` → `healthy`;
  same row +1h → `stalled`; missing ledger → `stalled`; DONE refused on stale.
- GitHub-hosted capacity is proven in this repo: admission/profile jobs of run
  37741732373 ran on `GitHub Actions 1000096421/22`; `nightly-...-github-hosted.yml`
  uses `runs-on: ubuntu-latest`.
