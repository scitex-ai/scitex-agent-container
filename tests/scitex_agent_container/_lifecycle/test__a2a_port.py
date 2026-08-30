"""Tests for ``_lifecycle._a2a_port`` (resolve + release).

PA-306: no `unittest.mock`. Production helpers `port_allocator.claim_port`
and `port_allocator.release_port` are swapped via a hand-rolled fake
context manager that records calls and restores the attribute on
teardown. The "end-to-end" tests use the REAL allocator against a REAL
database — no patching whatsoever. They took a ``tmp_path`` local DB file
until 2026-08-28, when ``a2a_ports`` moved to per-host PostgreSQL; they now
take the shared ``pg_schema`` fixture, which points the real store resolver
at a
throwaway schema and SKIPS where no writable database exists.
"""

from __future__ import annotations

from contextlib import contextmanager
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
    # Assert — an int the OPERATOR wrote in the spec is a PIN: a foreign holder
    # must raise, never be silently downgraded to some other port.
    assert rec.calls == [
        ((cfg.name,), {"explicit": 7901, "explicit_is_pin": True})
    ]  # stx-allow: STX-NL001


def test_reresolve_after_auto_claim_does_not_pass_it_off_as_an_operator_pin() -> None:
    # Arrange — THE RESTART PATH. `resolve_a2a_port` overwrites its own input
    # ("auto" -> the int it claimed), so the SECOND resolve (agent_start's
    # force/restart branch, after agent_stop released the row) sees an int and
    # is indistinguishable from an operator pin. It must NOT be treated as one:
    # a port we merely auto-allocated is a preference, and if it was taken while
    # we were down the agent must come back on a FRESH port rather than die.
    # This is the provenance loss that sent a routine restart down the
    # pinned-port TOCTOU that ghosted v0.21.18/19.
    cfg = _cfg("auto")  # stx-allow: STX-NL001
    rec = _Recorder().configured_to_return(19000)  # stx-allow: STX-NL001

    # Act — resolve twice against the SAME config object, as agent_start does.
    with _swap("claim_port", rec):
        resolve_a2a_port(cfg)
        resolve_a2a_port(cfg)

    # Assert
    assert rec.calls[1] == (
        (cfg.name,),
        {"explicit": 19000, "explicit_is_pin": False},
    )  # stx-allow: STX-NL001


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


def test_resolve_end_to_end_assigns_int_port(pg_schema: str) -> None:
    """No fakes — drive the real port_allocator against a real database.

    The claim ledger moved off ``state.db`` on 2026-08-28, so pinning
    ``state_db.DEFAULT_DB_PATH`` at a tmp file no longer isolates anything
    the allocator reads. ``pg_schema`` points the REAL store resolver at a
    throwaway schema instead. ``DEFAULT_RANGE`` is still saved and restored
    by hand (PA-306 — no monkeypatch).
    """
    # Arrange
    cfg = _cfg("auto")
    lo, hi = 26000, 26100  # stx-allow: STX-NL001
    saved_range = port_allocator.DEFAULT_RANGE
    port_allocator.DEFAULT_RANGE = (lo, hi)
    # Act
    try:
        resolve_a2a_port(cfg)
    finally:
        port_allocator.DEFAULT_RANGE = saved_range
    # Assert
    assert isinstance(cfg.a2a.port, int)


def test_resolve_end_to_end_port_within_configured_range(pg_schema: str) -> None:
    """No fakes — drive the real port_allocator against a real database."""
    # Arrange
    cfg = _cfg("auto")
    lo, hi = 26000, 26100  # stx-allow: STX-NL001
    saved_range = port_allocator.DEFAULT_RANGE
    port_allocator.DEFAULT_RANGE = (lo, hi)
    # Act
    try:
        resolve_a2a_port(cfg)
    finally:
        port_allocator.DEFAULT_RANGE = saved_range
    # Assert
    assert lo <= cfg.a2a.port <= hi
