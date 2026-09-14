from __future__ import annotations

import signal

import pytest

from scitex_agent_container.runtimes._inbox_sidecar_reconcile import (
    CURRENT_MODULE,
    CURRENT_ROLE,
    LEGACY_MODULE,
    LEGACY_PID_FILENAME,
    reconcile_inbox_sidecars,
)


class _Process:
    def __init__(self, pid, module, *, name="scholar", incarnation=None, role=None):
        argv = [
            "python",
            "-m",
            module,
            "--name",
            name,
            "--listen-url",
            "http://127.0.0.1:7878",
            "--turn-url",
            "http://127.0.0.1:19001/v1/turn",
            "--config-path",
            "/spec/scholar.yaml",
        ]
        if role is not None:
            argv += ["--process-role", role]
        if incarnation is not None:
            argv += ["--incarnation-id", incarnation]
        self.info = {
            "pid": pid,
            "cmdline": argv,
            "environ": {},
            "create_time": float(pid),
        }


def test_reconcile_preserves_authoritative_incarnation_and_retires_duplicates(tmp_path):
    # Arrange: one owner, both historical failure shapes, and two foreign rows.
    processes = [
        _Process(10, CURRENT_MODULE, incarnation="inc-now", role=CURRENT_ROLE),
        _Process(11, CURRENT_MODULE, incarnation="inc-old", role=CURRENT_ROLE),
        _Process(12, CURRENT_MODULE, incarnation="inc-now", role=CURRENT_ROLE),
        _Process(13, LEGACY_MODULE),
        _Process(14, CURRENT_MODULE, name="writer", incarnation="inc-old"),
        _Process(15, CURRENT_MODULE, incarnation="inc-old", role="other-role"),
        _Process(16, CURRENT_MODULE),
    ]
    killed = set()
    signals = []

    def process_iter():
        return [process for process in processes if process.info["pid"] not in killed]

    def send(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGKILL:
            killed.add(pid)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy_pidfile = state_dir / LEGACY_PID_FILENAME
    legacy_pidfile.write_text("13\n", encoding="utf-8")

    # Act
    receipts = reconcile_inbox_sidecars(
        name="scholar",
        config_path="/spec/scholar.yaml",
        incarnation_id="inc-now",
        authoritative_pid=10,
        state_dir=state_dir,
        process_iter=process_iter,
        signal_fn=send,
        sleep_fn=lambda _seconds: None,
    )

    # Assert
    by_pid = {receipt.pid: receipt for receipt in receipts}
    outcome = (
        set(by_pid),
        by_pid[10].disposition,
        tuple(by_pid[pid].reason for pid in (11, 12, 13, 16)),
        signals,
        legacy_pidfile.exists(),
    )
    assert outcome == (
        {10, 11, 12, 13, 16},
        "preserved",
        (
            "stale-or-duplicate-current-role",
            "stale-or-duplicate-current-role",
            "retired-legacy-role",
            "stale-or-duplicate-current-role",
        ),
        [
            (11, signal.SIGTERM),
            (12, signal.SIGTERM),
            (13, signal.SIGTERM),
            (16, signal.SIGTERM),
            (11, signal.SIGKILL),
            (12, signal.SIGKILL),
            (13, signal.SIGKILL),
            (16, signal.SIGKILL),
        ],
        False,
    )


def test_prelaunch_shape_retires_even_matching_incarnation_when_no_owner():
    # Arrange
    # An agent start runs after singleton teardown: no pid is authoritative.
    process = _Process(
        21, CURRENT_MODULE, incarnation="inc-now", role=CURRENT_ROLE
    )
    alive = True

    def process_iter():
        return [process] if alive else []

    def send(_pid, _sig):
        nonlocal alive
        alive = False

    # Act
    receipts = reconcile_inbox_sidecars(
        name="scholar",
        config_path="/spec/scholar.yaml",
        incarnation_id="inc-now",
        process_iter=process_iter,
        signal_fn=send,
        sleep_fn=lambda _seconds: None,
    )

    # Assert
    assert (receipts[0].disposition, receipts[0].signals) == (
        "retired",
        (signal.SIGTERM,),
    )


def test_reconcile_fails_loud_if_exact_stale_identity_survives_sigkill():
    # Arrange
    process = _Process(31, LEGACY_MODULE)

    # Act
    # Assert
    with pytest.raises(RuntimeError, match="31"):
        reconcile_inbox_sidecars(
            name="scholar",
            config_path="/spec/scholar.yaml",
            incarnation_id="inc-now",
            process_iter=lambda: [process],
            signal_fn=lambda _pid, _sig: None,
            sleep_fn=lambda _seconds: None,
        )
