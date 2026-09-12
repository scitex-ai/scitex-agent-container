from __future__ import annotations

from scitex_dev.status import StatusCode, new_exchange_id

from scitex_agent_container._lifecycle._stop_outcome import (
    StopVerificationError,
    record_stop_outcome,
    verify_tui_incarnation_stopped,
)


class _AbsentTui:
    def is_running(self, _config) -> bool:
        return False


def _instance() -> dict:
    return {
        "id": "01991c62-61ab-7abc-8000-000000000001",
        "host": "scitex-compute-03",
        "process_start_time": 17,
        "process_uid": 1000,
        "control_group": "/user.slice/tmux-spawn-a.scope",
        "scope_invocation_id": "a" * 32,
        "scope_unit": "tmux-spawn-a.scope",
    }


def test_pre_ownership_incarnation_ledgers_process_three_without_signal() -> None:
    # Arrange — exact shape read from the legacy central row on compute-03.
    instance = {
        "id": "f571b488-8683-4e57-a6f6-1a12870226c4",
        "host": "scitex-compute-03",
        "name": "scitex-app",
        "pid": 904612,
        "screen": "tui-scitex-app",
        "process_start_time": None,
        "process_uid": None,
        "control_group": None,
        "scope_unit": None,
        "scope_invocation_id": None,
    }
    outcomes: list[dict] = []
    signalled: list[bool] = []

    # Act
    try:
        verify_tui_incarnation_stopped(
            name="scitex-app",
            instance=instance,
            runtime_stop_succeeded=False,
            runtime=_AbsentTui(),
            config=object(),
            ensure_scope_down=lambda _row: signalled.append(True),
            outcome_recorder=lambda **fields: outcomes.append(fields),
        )
    except Exception as exc:  # noqa: BLE001 - assertion names exact type
        error = exc
    else:
        error = None
    # Assert
    assert (
        isinstance(error, StopVerificationError),
        outcomes[0]["status"].code,
        "process_start_time" in outcomes[0]["status"].message,
        signalled,
    ) == (True, 3, True, [])


def test_absent_tmux_with_surviving_scope_fails_even_force_shape() -> None:
    # Arrange
    outcomes: list[dict] = []

    def record(**fields) -> None:
        outcomes.append(fields)

    # Act
    try:
        verify_tui_incarnation_stopped(
            name="scitex-app",
            instance=_instance(),
            runtime_stop_succeeded=False,
            runtime=_AbsentTui(),
            config=object(),
            ensure_scope_down=lambda _row: False,
            outcome_recorder=record,
        )
    except Exception as exc:  # noqa: BLE001 - assertion names exact type
        error = exc
    else:
        error = None
    # Assert
    assert (isinstance(error, StopVerificationError), outcomes[0]["status"].code) == (
        True,
        3,
    )


def test_process_zero_requires_observed_scope_disappearance() -> None:
    # Arrange
    outcomes: list[dict] = []

    def record(**fields) -> None:
        outcomes.append(fields)

    # Act
    exchange_id = verify_tui_incarnation_stopped(
        name="scitex-app",
        instance=_instance(),
        runtime_stop_succeeded=False,
        runtime=_AbsentTui(),
        config=object(),
        ensure_scope_down=lambda _row: True,
        outcome_recorder=record,
    )
    # Assert
    assert outcomes[0]["status"].code == 0 and exchange_id.startswith("xch_")


class _MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def put(self, row: dict, **_kwargs) -> None:
        self.rows.append(row)


def test_ledger_participant_carries_exact_incarnation_identity() -> None:
    # Arrange
    store = _MemoryStore()
    instance = _instance()
    # Act
    record_stop_outcome(
        name="scitex-app",
        instance=instance,
        status=StatusCode(kind="process", code=0, message="observed stopped"),
        exchange_id=new_exchange_id(host="test-host"),
        opened_at="2026-09-12T11:00:00+00:00",
        store=store,
    )
    # Assert
    assert store.rows[0]["responder"] == (
        "scitex-compute-03/scitex-agent-container/scitex-app/"
        "01991c62-61ab-7abc-8000-000000000001"
    )
