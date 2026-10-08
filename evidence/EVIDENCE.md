# IDs tied to results (audit items 2–3), verified 2026-10-08 ~09:30–09:40Z

## Code under test
- Watcher + exit-code tests: scitex-agent-container commit
  `7b278808ff85ff339f7e591848f721dad4754dac`
  (branch `hermes-subagent/subagent-sa-0-5b415209`).
  No CI run exists for that branch (`gh run list --branch ...` empty) —
  results below are local pytest + live API evidence.
- PG18 wrapper: `exec-in-sif.sh` + `sif-runtime-lib.sh` + `tmpdir-lib.sh`
  byte copies in `pgrepro/.github/ci/` from branch
  `hermes-subagent/subagent-sa-0-50f34f95` @ `2fc671283`
  (sha256: exec-in-sif ae215bce…, sif-runtime-lib c5fd9092…,
  tmpdir-lib 594b5b33…, run-in-sif 2f142110…).

## Item 2 — PG18 failure path (real run 37741732373, job 113196528186)
- GitHub: job `tests / pytest-matrix-on-ubuntu-py3.11` on runner `scitex-ci-03`
  → `status:completed conclusion:cancelled`; step
  `Run the genuine full leaf SIF suite` → `cancelled`.
  All 3 matrix legs (02/03/04) cancelled; admission/profile on GitHub-hosted → success.
  (Run still `queued` — stuck `ci-verdict-to-pushing-agent` job; job logs unavailable.)
- Wrapper: real wrapper with PG18 deliberately unsatisfied
  (PG SIF var pointed at non-PG18 image, valid sha so the digest gate passes)
  → stderr `::error::required owned PG18 unavailable`, **exit 1**.
  (On this host apptainer fails at image mount — no fuse — which trips the same
  fail-closed `ci_pg_start || { …; exit 1; }` path as a version-gate miss.)
  No `continue-on-error` anywhere in `.github/workflows/` → nonzero fails the step.
- Watcher: live `poll_once` (real `gh api`) on run 37741732373 →
  `status: failure` (cancelled legs fail closed), `health: failing`,
  `assert_done_allowed` raises `DoneRefused`.
  Row: `evidence/ci-watch.jsonl` (run_id 37741732373, recorded_at 2026-10-08T09:30:54Z).

## Item 3 — 4 acceptance tests (raw: evidence/acceptance-4.txt)
| # | Test (synthetic watched run_id "7") | Result |
|---|---|---|
| 1 | test_failed_run_is_recorded_with_agent_stopped | PASSED |
| 2 | test_stale_success_is_stalled_not_healthy | PASSED |
| 3 | test_done_refused_while_ci_failing | PASSED |
| 4 | test_done_allowed_when_required_ci_green | PASSED |
Full files: `test__ci_watcher.py` (22) + `test_ci_exit_code_propagation.py` (2)
→ **24 passed** with `/uvwork/venv-agent/bin/python -m pytest` (pytest 9.1.1).
Real-run companions: #1 ⇔ live poll of 37741732373 records failure with no
agent process; #2 ⇔ stale/missing ledger → stalled (live demo);
#3 ⇔ DONE refused on the live failure row; #4 ⇔ DONE allowed on fresh success row.
