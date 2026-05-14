"""Tests for ``_lifecycle._a2a_port`` (resolve + release)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from scitex_agent_container._lifecycle._a2a_port import (
    release_a2a_port,
    resolve_a2a_port,
)
from scitex_agent_container._state import port_allocator
from scitex_agent_container.config._types import A2ASpec, AgentConfig


def _cfg(port: int | str | None) -> AgentConfig:
    return AgentConfig(name=f"agent-{port}", a2a=A2ASpec(port=port))


def test_resolve_auto_claims_int_port(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    cfg = _cfg("auto")
    with patch.object(
        port_allocator,
        "claim_port",
        return_value=19042,  # stx-allow: STX-NL001
    ) as mock:
        resolve_a2a_port(cfg)
    mock.assert_called_once_with(cfg.name)
    assert cfg.a2a.port == 19042  # stx-allow: STX-NL001


def test_resolve_explicit_int_records_claim() -> None:
    cfg = _cfg(7901)  # stx-allow: STX-NL001
    with patch.object(
        port_allocator,
        "claim_port",
        return_value=7901,  # stx-allow: STX-NL001
    ) as mock:
        resolve_a2a_port(cfg)
    mock.assert_called_once_with(cfg.name, explicit=7901)  # stx-allow: STX-NL001
    assert cfg.a2a.port == 7901  # stx-allow: STX-NL001


def test_resolve_none_is_noop() -> None:
    cfg = _cfg(None)
    with patch.object(port_allocator, "claim_port") as mock:
        resolve_a2a_port(cfg)
    mock.assert_not_called()
    assert cfg.a2a.port is None


def test_release_calls_allocator() -> None:
    with patch.object(port_allocator, "release_port") as mock:
        release_a2a_port("alpha")
    mock.assert_called_once_with("alpha")


def test_resolve_zero_int_is_noop() -> None:
    # Port 0 is the legacy "no sidecar" signal — never auto-resolve.
    cfg = _cfg(0)
    with patch.object(port_allocator, "claim_port") as mock:
        resolve_a2a_port(cfg)
    mock.assert_not_called()


def test_resolve_end_to_end_uses_real_allocator(tmp_path: Path) -> None:
    """No mocks — verify the allocator actually claims through state.db."""
    db = tmp_path / "state.db"
    cfg = _cfg("auto")
    lo, hi = 26000, 26100  # stx-allow: STX-NL001
    with patch.object(port_allocator, "DEFAULT_RANGE", (lo, hi)):
        with patch(
            "scitex_agent_container._state.state_db.DEFAULT_DB_PATH",
            db,
        ):
            resolve_a2a_port(cfg)
    assert isinstance(cfg.a2a.port, int)
    assert lo <= cfg.a2a.port <= hi
