"""Native Codex TUI admission selects start vs immediate steer."""

from __future__ import annotations

import pytest

from scitex_agent_container.runtimes._codex_tui_native import _admit_with_rpc


class _Rpc:
    def __init__(self, status: str, turns: list[dict]) -> None:
        self.thread = {"id": "thread-1", "status": {"type": status}, "turns": turns}
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "thread/list":
            return {"data": [self.thread]}
        if method == "thread/read":
            return {"thread": self.thread}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/start":
            return {"turn": {"id": "turn-new"}}
        raise AssertionError(f"unexpected method {method}")


@pytest.mark.asyncio
async def test_active_codex_turn_is_steered_immediately() -> None:
    # Arrange
    rpc = _Rpc("active", [{"id": "turn-active", "status": "inProgress"}])

    # Act
    admitted = await _admit_with_rpc(rpc, agent="agent", text="new instruction")

    # Assert
    assert admitted.mode == "steer"


@pytest.mark.asyncio
async def test_idle_codex_thread_starts_a_visible_turn() -> None:
    # Arrange
    rpc = _Rpc("idle", [])

    # Act
    admitted = await _admit_with_rpc(rpc, agent="agent", text="new instruction")

    # Assert
    assert admitted.mode == "start"


@pytest.mark.asyncio
async def test_ambiguous_active_turn_fails_without_fallback() -> None:
    # Arrange
    rpc = _Rpc("active", [])

    # Act
    with pytest.raises(RuntimeError, match="refusing ambiguous steer"):
        await _admit_with_rpc(rpc, agent="agent", text="do not queue me")

    # Assert
    assert all(call[0] not in {"turn/start", "turn/steer"} for call in rpc.calls)
