"""Tests for the agent-independent CI-run watcher with DONE-gating.

The watcher polls a target workflow run's JOB + STEP states on its own
schedule, records one ledger row per poll, and gates DONE on a fresh
success. No test below constructs an agent, a listener, or a broker:
that absence is the point — detection must not depend on agents being
alive.

Conventions: one assertion per test (STX-TQ007); AAA markers; no mocks /
monkeypatch (STX-NM) — ``fetch`` / ``now_fn`` / ``sleep_fn`` doubles are
dependency-injected callables.
"""

from __future__ import annotations

import json

import pytest

from scitex_agent_container._lifecycle._ci_watcher import (
    DoneRefused,
    assert_done_allowed,
    fetch_host_loads,
    fetch_run_jobs,
    health,
    idle_while_queued,
    parse_host_probe,
    poll_host_loads_once,
    poll_once,
    read_latest_host_load,
    record_snapshot,
    roll_up_status,
    watch_forever,
)

T0 = 1_786_000_000.0


def _jobs_payload(jobs):
    """A ``fetch`` double answering the jobs API with canned job states."""

    def fetch(args):
        assert "actions/runs/" in args[1]
        return json.dumps({"jobs": jobs})

    return fetch


def _completed(name, conclusion, steps=()):
    job = {"name": name, "status": "completed", "conclusion": conclusion}
    if steps:
        job["steps"] = list(steps)
    return job


def _step(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


# --- roll-up ---------------------------------------------------------------


def test_all_success_jobs_roll_up_to_success():
    # Arrange — every job green, every step green.
    jobs = [_completed("lint", "success"), _completed("pytest", "success")]
    # Act
    status, _, _ = roll_up_status(jobs)
    # Assert
    assert status == "success"


def test_any_failed_job_dominates_to_failure():
    # Arrange — one red job among greens.
    jobs = [_completed("lint", "success"), _completed("pytest", "failure")]
    # Act
    status, _, _ = roll_up_status(jobs)
    # Assert
    assert status == "failure"


def test_in_progress_job_rolls_up_to_running():
    # Arrange — a job still executing, nothing failed.
    jobs = [
        _completed("lint", "success"),
        {"name": "pytest", "status": "in_progress", "conclusion": None},
    ]
    # Act
    status, _, _ = roll_up_status(jobs)
    # Assert
    assert status == "running"


def test_empty_job_list_is_unknown_never_green():
    # Arrange — GitHub returned no jobs (run not found yet, partial read).
    # Act
    status, _, _ = roll_up_status([])
    # Assert
    assert status == "unknown"


def test_unparseable_payload_is_unknown_never_green():
    # Arrange — garbage from the fetch seam.
    # Act
    status, _, _ = roll_up_status(None)
    # Assert
    assert status == "unknown"


def test_cancelled_job_counts_as_failure_not_a_pass():
    # Arrange — a cancelled run is unfinished work whose verdict nobody has.
    jobs = [_completed("lint", "success"), _completed("pytest", "cancelled")]
    # Act
    status, _, _ = roll_up_status(jobs)
    # Assert
    assert status == "failure"


def test_failed_step_under_success_job_fails_closed():
    # Arrange — job claims success while one of its steps failed.
    jobs = [
        _completed(
            "pytest",
            "success",
            steps=[_step("ok"), _step("red", conclusion="failure")],
        )
    ]
    # Act
    status, per_job, _ = roll_up_status(jobs)
    # Assert
    assert per_job["pytest"] == "failure"


def test_step_states_are_recorded_per_job():
    # Arrange — a finished job with two steps.
    fetch = _jobs_payload(
        [_completed("lint", "success", steps=[_step("a"), _step("b")])]
    )
    # Act
    _, _, per_step = fetch_run_jobs("o/r", 42, fetch=fetch)
    # Assert
    assert per_step["lint"] == {"a": "success", "b": "success"}


# --- acceptance 1: failure recorded with no agent in the loop -------------


def test_failed_run_is_recorded_with_agent_stopped(tmp_path):
    # Arrange — the detached watcher path (watch_forever, bounded by
    # stop_after for the test); no agent, listener, or broker exists here.
    ledger = tmp_path / "ci-watch.jsonl"
    fetch = _jobs_payload(
        [_completed("lint", "success"), _completed("pytest", "failure")]
    )
    # Act
    watch_forever(
        ledger,
        targets=[("o/r", 7)],
        fetch=fetch,
        now_fn=lambda: T0,
        sleep_fn=lambda s: None,
        stop_after=1,
    )
    # Assert — import json locally: one assertion, the recorded verdict.
    import json as _json

    assert _json.loads(ledger.read_text().strip())["status"] == "failure"


def test_failed_record_carries_the_watched_run_id(tmp_path):
    # Arrange — a failing poll recorded at a known instant.
    ledger = tmp_path / "ci-watch.jsonl"
    fetch = _jobs_payload([_completed("pytest", "failure")])
    # Act
    row = poll_once(
        ledger, repo="o/r", run_id=7, fetch=fetch, now_fn=lambda: T0
    )
    # Assert
    assert row["run_id"] == "7"


def test_failed_record_carries_a_timestamp(tmp_path):
    # Arrange — a failing poll recorded at a known instant.
    ledger = tmp_path / "ci-watch.jsonl"
    fetch = _jobs_payload([_completed("pytest", "failure")])
    # Act
    row = poll_once(
        ledger, repo="o/r", run_id=7, fetch=fetch, now_fn=lambda: T0
    )
    # Assert
    assert row["recorded_at"].startswith("2026-")


def test_unreadable_run_is_recorded_unknown_not_dropped(tmp_path):
    # Arrange — gh output is garbage (network blip, API change).
    ledger = tmp_path / "ci-watch.jsonl"
    # Act
    row = poll_once(
        ledger,
        repo="o/r",
        run_id=7,
        fetch=lambda args: "not json{{{",
        now_fn=lambda: T0,
    )
    # Assert
    assert row["status"] == "unknown"


def test_watcher_survives_a_raising_fetch_and_keeps_polling(tmp_path):
    # Arrange — the fetch seam explodes instead of returning.
    ledger = tmp_path / "ci-watch.jsonl"

    def boom(args):
        raise OSError("network down")

    # Act
    polls = watch_forever(
        ledger,
        targets=[("o/r", 7)],
        fetch=boom,
        now_fn=lambda: T0,
        sleep_fn=lambda s: None,
        stop_after=2,
    )
    # Assert
    assert polls == 2


# --- acceptance 2: stopped runner reads stalled, never healthy -------------


def test_stale_success_is_stalled_not_healthy(tmp_path):
    # Arrange — the watcher recorded green, then the runner went silent.
    ledger = tmp_path / "ci-watch.jsonl"
    record_snapshot(
        ledger, repo="o/r", run_id=7, status="success", now_fn=lambda: T0
    )
    # Act — health is asked long after the staleness bound.
    result = health(
        ledger,
        repo="o/r",
        run_id=7,
        stale_after_s=600.0,
        now_fn=lambda: T0 + 3600.0,
    )
    # Assert
    assert result == "stalled"


def test_missing_ledger_is_stalled_not_healthy(tmp_path):
    # Arrange — the watcher never polled this run at all.
    # Act
    result = health(
        tmp_path / "absent.jsonl", repo="o/r", run_id=7, now_fn=lambda: T0
    )
    # Assert
    assert result == "stalled"


def test_fresh_failure_reads_failing_not_healthy(tmp_path):
    # Arrange — a fresh red record.
    ledger = tmp_path / "ci-watch.jsonl"
    record_snapshot(
        ledger, repo="o/r", run_id=7, status="failure", now_fn=lambda: T0
    )
    # Act
    result = health(ledger, repo="o/r", run_id=7, now_fn=lambda: T0 + 10.0)
    # Assert
    assert result == "failing"


def test_fresh_success_reads_healthy(tmp_path):
    # Arrange — a fresh green record.
    ledger = tmp_path / "ci-watch.jsonl"
    record_snapshot(
        ledger, repo="o/r", run_id=7, status="success", now_fn=lambda: T0
    )
    # Act
    result = health(ledger, repo="o/r", run_id=7, now_fn=lambda: T0 + 10.0)
    # Assert
    assert result == "healthy"


# --- acceptance 3: DONE refused while CI is failing --------------------------


def test_done_refused_while_ci_failing(tmp_path):
    # Arrange — latest record is a fresh failure.
    ledger = tmp_path / "ci-watch.jsonl"
    record_snapshot(
        ledger, repo="o/r", run_id=7, status="failure", now_fn=lambda: T0
    )
    # Act + Assert — the gate raises instead of permitting DONE.
    with pytest.raises(DoneRefused):
        assert_done_allowed(ledger, repo="o/r", run_id=7, now_fn=lambda: T0)


def test_done_refused_while_ci_running(tmp_path):
    # Arrange — CI still executing: incomplete is not permission.
    ledger = tmp_path / "ci-watch.jsonl"
    record_snapshot(
        ledger, repo="o/r", run_id=7, status="running", now_fn=lambda: T0
    )
    # Act + Assert
    with pytest.raises(DoneRefused):
        assert_done_allowed(ledger, repo="o/r", run_id=7, now_fn=lambda: T0)


def test_done_refused_with_no_record_unverifiable(tmp_path):
    # Arrange — nothing was ever recorded for this run.
    # Act + Assert — absence of evidence is not permission.
    with pytest.raises(DoneRefused):
        assert_done_allowed(
            tmp_path / "absent.jsonl", repo="o/r", run_id=7, now_fn=lambda: T0
        )


def test_done_refused_when_record_is_stale(tmp_path):
    # Arrange — green once, then silence past the staleness bound.
    ledger = tmp_path / "ci-watch.jsonl"
    record_snapshot(
        ledger, repo="o/r", run_id=7, status="success", now_fn=lambda: T0
    )
    # Act + Assert — yesterday's green does not certify today's HEAD.
    with pytest.raises(DoneRefused):
        assert_done_allowed(
            ledger,
            repo="o/r",
            run_id=7,
            stale_after_s=600.0,
            now_fn=lambda: T0 + 3600.0,
        )


# --- acceptance 4: DONE allowed when required CI is green --------------------


def test_done_allowed_when_required_ci_green(tmp_path):
    # Arrange — latest record is a fresh success.
    ledger = tmp_path / "ci-watch.jsonl"
    record_snapshot(
        ledger, repo="o/r", run_id=7, status="success", now_fn=lambda: T0
    )
    # Act
    row = assert_done_allowed(ledger, repo="o/r", run_id=7, now_fn=lambda: T0)
    # Assert
    assert row["status"] == "success"


# --- host load: probe parsing ------------------------------------------------


def test_probe_output_parses_load_and_process_counts():
    # Arrange — one host's four probe lines (loadavg + 3 counts).
    # Act
    snap = parse_host_probe("0.50 0.52 0.44 1/1245 3038435\n2\n15\n0\n")
    # Assert
    assert (snap["load1"], snap["runner"], snap["apptainer"], snap["pytest"]) == (
        0.50,
        2,
        15,
        0,
    )


def test_low_load_without_pytest_reads_idle():
    # Arrange — load 0.5, no test procs (compute-02 shape, 2026-10-08).
    # Act
    snap = parse_host_probe("0.50 0.52 0.44 1/1245 1\n2\n15\n0\n")
    # Assert
    assert snap["idle"] is True


def test_running_pytest_is_not_idle_despite_low_load():
    # Arrange — load 1.4 but test procs present (compute-04 shape).
    # Act
    snap = parse_host_probe("1.42 1.53 1.45 2/1544 1\n4\n26\n11\n")
    # Assert
    assert snap["idle"] is False


def test_garbage_probe_is_error_never_idle():
    # Arrange — ssh printed nothing usable.
    # Act
    snap = parse_host_probe("not a probe\n")
    # Assert
    assert snap.get("idle") is not True and "error" in snap


def test_ssh_failure_records_error_never_idle():
    # Arrange — the ssh seam explodes instead of returning.
    def boom(host, cmd):
        raise OSError("no route to host")

    # Act
    snaps = fetch_host_loads(["scitex-compute-02"], ssh=boom)
    # Assert
    assert "error" in snaps["scitex-compute-02"]


# --- host load: idle-while-queued flag ---------------------------------------


def test_flag_true_when_queued_and_all_hosts_idle():
    # Arrange — queued work exists; every probed host idle.
    hosts = {"h2": {"idle": True}, "h3": {"idle": True}}
    # Act
    result = idle_while_queued(hosts, True)
    # Assert
    assert result is True


def test_flag_false_without_queued_work():
    # Arrange — idle fleet but nothing queued.
    hosts = {"h2": {"idle": True}, "h3": {"idle": True}}
    # Act
    result = idle_while_queued(hosts, False)
    # Assert
    assert result is False


def test_flag_false_when_one_host_busy():
    # Arrange — queued work, but one host runs tests.
    hosts = {"h2": {"idle": True}, "h4": {"idle": False}}
    # Act
    result = idle_while_queued(hosts, True)
    # Assert
    assert result is False


def test_host_poll_records_own_ledger_row_with_flag(tmp_path):
    # Arrange — a canned fleet: one idle host, queued work waiting.
    ledger = tmp_path / "ci-watch-host-load.jsonl"

    def ssh(host, cmd):
        return "0.50 0.52 0.44 1/10 1\n2\n15\n0\n"

    # Act
    row = poll_host_loads_once(
        ledger, queued=True, hosts=["h2"], ssh=ssh, now_fn=lambda: T0
    )
    # Assert
    assert (row["type"], row["idle_while_queued"]) == ("host-load", True)


def test_latest_host_row_is_readable_back(tmp_path):
    # Arrange — one recorded host-load poll.
    ledger = tmp_path / "ci-watch-host-load.jsonl"

    def ssh(host, cmd):
        return "0.50 0.52 0.44 1/10 1\n2\n15\n0\n"

    poll_host_loads_once(
        ledger, queued=True, hosts=["h2"], ssh=ssh, now_fn=lambda: T0
    )
    # Act
    latest = read_latest_host_load(ledger)
    # Assert
    assert latest is not None and latest["hosts"]["h2"]["idle"] is True
