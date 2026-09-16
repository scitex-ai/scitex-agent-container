"""The reconciler turns expired durable obligations into sender notices."""

from __future__ import annotations

import pytest

from scitex_agent_container._listen._delegation_reconciler import reconcile_once
from scitex_agent_container._state import delegation_lifecycle


@pytest.mark.asyncio
async def test_reconciler_publishes_correlated_notice_then_marks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = {
        "action": delegation_lifecycle.RECEIPT_NUDGE_SENT,
        "dispatch_id": "dispatch-1",
        "correlation_id": "xch_correlation",
        "lineage_id": "xch_lineage",
        "sender": "lead",
        "assignee": "worker",
    }
    marked: list[tuple[str, str]] = []
    published: list[dict] = []

    monkeypatch.setattr(
        delegation_lifecycle, "due_supervision_actions", lambda **_kwargs: [action]
    )
    monkeypatch.setattr(
        delegation_lifecycle,
        "record_stage",
        lambda dispatch_id, stage, **_kwargs: marked.append((dispatch_id, stage)),
    )

    async def publish(**kwargs):
        published.append(kwargs)
        return {"durably_queued": True}

    result = await reconcile_once(publish=publish, now=200.0)

    assert result[0]["notice_id"] == "dispatch-1:receipt_nudge_sent"
    assert published[0]["agent"] == "lead"
    assert marked == [("dispatch-1", delegation_lifecycle.RECEIPT_NUDGE_SENT)]


@pytest.mark.asyncio
async def test_reconciler_does_not_mark_a_notice_that_failed_to_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = {
        "action": delegation_lifecycle.FAILURE_REPORTED,
        "dispatch_id": "dispatch-2",
        "correlation_id": "xch_correlation",
        "lineage_id": "xch_lineage",
        "sender": "lead",
        "assignee": "worker",
    }
    marked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        delegation_lifecycle, "due_supervision_actions", lambda **_kwargs: [action]
    )
    monkeypatch.setattr(
        delegation_lifecycle,
        "record_stage",
        lambda dispatch_id, stage, **_kwargs: marked.append((dispatch_id, stage)),
    )

    async def publish(**_kwargs):
        raise ConnectionError("postgres channel store unavailable")

    with pytest.raises(ConnectionError, match="postgres"):
        await reconcile_once(publish=publish, now=200.0)
    assert marked == []
