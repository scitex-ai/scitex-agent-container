"""Contract tests for the durable A2A lifecycle state machine."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container._state.delegation_lifecycle import (
    AGENT_OBSERVED,
    AGENT_QUEUE_ACCEPTED,
    COMPLETED,
    FAILED,
    PROGRESS_NUDGE_SENT,
    RECEIPT_NUDGE_SENT,
    due_supervision_actions,
    lifecycle_rows,
    new_correlation_id,
    open_lifecycle,
    record_stage,
    validated_reverse_route,
)


class _MemoryStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict] = {}

    def rows(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(values=value) for value in self.values.values()]

    def put(self, values: dict, *, expected_revision: object) -> None:
        del expected_revision
        key = (str(values["dispatch_id"]), str(values["stage"]))
        if key in self.values:
            raise RuntimeError("duplicate identity")
        self.values[key] = dict(values)

    def close(self) -> None:
        return

    def factory(self) -> _MemoryStore:
        return self


def _open(memory: _MemoryStore, *, now: float = 100.0) -> tuple[str, str]:
    correlation = new_correlation_id()
    lineage = new_correlation_id()
    open_lifecycle(
        dispatch_id="dispatch-1",
        correlation_id=correlation,
        lineage_id=lineage,
        kind="delegation",
        sender="lead",
        assignee="worker",
        reverse_route="https://listen.example/agents/lead/message:send",
        receipt_deadline_at=110.0,
        progress_deadline_at=130.0,
        now=now,
        _store_factory=memory.factory,
    )
    return correlation, lineage


def test_open_persists_lineage_route_and_responsibility_transfer() -> None:
    memory = _MemoryStore()
    correlation, lineage = _open(memory)

    row = lifecycle_rows(_store_factory=memory.factory)[0]

    assert (
        row["correlation_id"],
        row["lineage_id"],
        row["task_responsibility"],
        row["execution_responsibility"],
        row["supervision_responsibility"],
    ) == (correlation, lineage, "worker", "worker", "lead")
    assert row["reverse_route"].endswith("/agents/lead/message:send")


def test_stages_are_append_only_and_terminal_is_immutable() -> None:
    memory = _MemoryStore()
    _open(memory)
    assert record_stage(
        "dispatch-1", AGENT_QUEUE_ACCEPTED, now=101, _store_factory=memory.factory
    )
    assert record_stage(
        "dispatch-1", AGENT_OBSERVED, now=102, _store_factory=memory.factory
    )
    assert record_stage("dispatch-1", COMPLETED, now=103, _store_factory=memory.factory)
    assert not record_stage(
        "dispatch-1", COMPLETED, now=104, _store_factory=memory.factory
    )
    with pytest.raises(RuntimeError, match="terminal"):
        record_stage("dispatch-1", FAILED, _store_factory=memory.factory)

    assert [row["stage"] for row in lifecycle_rows(_store_factory=memory.factory)] == [
        "store_accepted",
        AGENT_QUEUE_ACCEPTED,
        AGENT_OBSERVED,
        COMPLETED,
    ]


def test_deadlines_distinguish_missing_receipt_from_missing_progress() -> None:
    memory = _MemoryStore()
    _open(memory)
    receipt = due_supervision_actions(now=111, _store_factory=memory.factory)
    assert [item["action"] for item in receipt] == [RECEIPT_NUDGE_SENT]

    record_stage("dispatch-1", AGENT_OBSERVED, now=112, _store_factory=memory.factory)
    progress = due_supervision_actions(now=131, _store_factory=memory.factory)
    assert [item["action"] for item in progress] == [PROGRESS_NUDGE_SENT]


@pytest.mark.parametrize(
    "url",
    ["listen.example", "ftp://listen.example", "https://user:secret@listen.example"],
)
def test_reverse_route_rejects_ambiguous_or_secret_bearing_urls(url: str) -> None:
    with pytest.raises(ValueError, match="reverse route"):
        validated_reverse_route(sender="lead", listen_url=url)
