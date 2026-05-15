"""Tests for ``_lifecycle._a2a_port`` (resolve + release).

PA-306: no `unittest.mock`. Production helpers `port_allocator.claim_port`
and `port_allocator.release_port` are swapped via a hand-rolled fake
context manager that records calls and restores the attribute on
teardown. The "end-to-end" test uses the REAL allocator against a
``tmp_path`` sqlite DB — no patching whatsoever.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle._a2a_port import (
    release_a2a_port,
    resolve_a2a_port,
)
from scitex_agent_container._state import port_allocator
from scitex_agent_container.config._types import A2ASpec, AgentConfig


def _cfg(port: int | str | None) -> AgentConfig:
    return AgentConfig(name=f"agent-{port}", a2a=A2ASpec(port=port))


class _Recorder:
    """Records (args, kwargs) per call. Replaces ``mock.assert_called_with``."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        self.calls.append((args, kwargs))
        return self._return_value

    def configured_to_return(self, value: int) -> "_Recorder":
        self._return_value = value
        return self


@contextmanager
def _swap(name: str, fn: Any) -> Iterator[None]:
    saved = getattr(port_allocator, name)
    setattr(port_allocator, name, fn)
    try:
        yield
    finally:
        setattr(port_allocator, name, saved)


def test_resolve_auto_invokes_claim_with_agent_name() -> None:
    # Arrange
    cfg = _cfg("auto")
    rec = _Recorder().configured_to_return(19042)  # stx-allow: STX-NL001
    # Act
    with _swap("claim_port", rec):
        resolve_a2a_port(cfg)
    # Assert
    assert rec.calls == [((cfg.name,), {})]


def test_resolve_auto_assigns_claimed_port_to_cfg() -> None:
    # Arrange
    cfg = _cfg("auto")
    rec = _Recorder().configured_to_return(19042)  # stx-allow: STX-NL001
    # Act
    with _swap("claim_port", rec):
        resolve_a2a_port(cfg)
    # Assert
    assert cfg.a2a.port == 19042  # stx-allow: STX-NL001


def test_resolve_explicit_int_invokes_claim_with_explicit_kwarg() -> None:
    # Arrange
    cfg = _cfg(7901)  # stx-allow: STX-NL001
    rec = _Recorder().configured_to_return(7901)  # stx-allow: STX-NL001
    # Act
    with _swap("claim_port", rec):
        resolve_a2a_port(cfg)
    # Assert
    assert rec.calls == [((cfg.name,), {"explicit": 7901})]  # stx-allow: STX-NL001


def test_resolve_explicit_int_assigns_claimed_port_to_cfg() -> None:
    # Arrange
    cfg = _cfg(7901)  # stx-allow: STX-NL001
    rec = _Recorder().configured_to_return(7901)  # stx-allow: STX-NL001
    # Act
    with _swap("claim_port", rec):
        resolve_a2a_port(cfg)
    # Assert
    assert cfg.a2a.port == 7901  # stx-allow: STX-NL001


@pytest.mark.parametrize(
    "port_value",
    [
        pytest.param(None, id="none"),
        # Port 0 is the legacy "no sidecar" signal — never auto-resolve.
        pytest.param(0, id="zero"),
    ],
)
def test_resolve_noop_values_do_not_invoke_claim(port_value: int | None) -> None:
    # Arrange
    cfg = _cfg(port_value)
    rec = _Recorder().configured_to_return(0)
    # Act
    with _swap("claim_port", rec):
        resolve_a2a_port(cfg)
    # Assert
    assert rec.calls == []


def test_resolve_none_leaves_port_unchanged() -> None:
    # Arrange
    cfg = _cfg(None)
    rec = _Recorder().configured_to_return(0)
    # Act
    with _swap("claim_port", rec):
        resolve_a2a_port(cfg)
    # Assert
    assert cfg.a2a.port is None


def test_release_calls_allocator() -> None:
    # Arrange
    rec = _Recorder().configured_to_return(0)
    # Act
    with _swap("release_port", rec):
        release_a2a_port("alpha")
    # Assert
    assert rec.calls == [(("alpha",), {})]


def test_resolve_end_to_end_assigns_int_port(tmp_path: Path) -> None:
    """No fakes — drive the real port_allocator against a tmp sqlite DB."""
    # Arrange
    db = tmp_path / "state.db"
    cfg = _cfg("auto")
    lo, hi = 26000, 26100  # stx-allow: STX-NL001
    from scitex_agent_container._state import state_db

    saved_range = port_allocator.DEFAULT_RANGE
    saved_db = state_db.DEFAULT_DB_PATH
    port_allocator.DEFAULT_RANGE = (lo, hi)
    state_db.DEFAULT_DB_PATH = db
    # Act
    try:
        resolve_a2a_port(cfg)
    finally:
        port_allocator.DEFAULT_RANGE = saved_range
        state_db.DEFAULT_DB_PATH = saved_db
    # Assert
    assert isinstance(cfg.a2a.port, int)


def test_resolve_end_to_end_port_within_configured_range(tmp_path: Path) -> None:
    """No fakes — drive the real port_allocator against a tmp sqlite DB."""
    # Arrange
    db = tmp_path / "state.db"
    cfg = _cfg("auto")
    lo, hi = 26000, 26100  # stx-allow: STX-NL001
    from scitex_agent_container._state import state_db

    saved_range = port_allocator.DEFAULT_RANGE
    saved_db = state_db.DEFAULT_DB_PATH
    port_allocator.DEFAULT_RANGE = (lo, hi)
    state_db.DEFAULT_DB_PATH = db
    # Act
    try:
        resolve_a2a_port(cfg)
    finally:
        port_allocator.DEFAULT_RANGE = saved_range
        state_db.DEFAULT_DB_PATH = saved_db
    # Assert
    assert lo <= cfg.a2a.port <= hi
