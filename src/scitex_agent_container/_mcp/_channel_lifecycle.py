"""Project inbound structural receipts onto the durable A2A lifecycle."""

from __future__ import annotations

import json
from typing import Any


def absorb_completion_stage(event: dict[str, Any]) -> bool:
    """Persist a completion event as ``completed`` or ``failed``.

    Returns ``False`` for ordinary/legacy events. Store failures propagate so
    the durable channel event remains replayable instead of being acknowledged
    while its terminal lifecycle evidence is lost.
    """
    if event.get("kind") != "completion":
        return False
    dispatch_id = event.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return False
    content = event.get("content")
    if not isinstance(content, str):
        return False
    try:
        report = json.loads(content)
    except json.JSONDecodeError:
        return False
    if not isinstance(report, dict):
        return False
    status = report.get("status")
    from .._state.delegation_lifecycle import COMPLETED, FAILED, record_stage

    stage = COMPLETED if status == "success" else FAILED
    try:
        # Never copy peer-authored content/summary into lifecycle state: it may
        # contain credentials. The channel event remains the content record;
        # this projection stores protocol state only.
        record_stage(dispatch_id, stage, detail=f"completion status={status}")
    except LookupError:
        return False
    return True


__all__ = ["absorb_completion_stage"]
