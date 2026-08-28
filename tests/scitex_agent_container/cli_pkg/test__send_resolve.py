"""Tests for ``scitex_agent_container.cli_pkg._send_resolve``.

The live-endpoint resolver behind the registry split-brain fix.
``agent_send`` resolves a target's ``/v1/turn`` endpoint through this
module, which prefers the active ``instances`` row port but falls back
to the DURABLE ``port_allocator`` claim — the source that stays correct
when a health-monitor restart (``runtime.start`` directly) leaves the
``instances`` row stale while the agent keeps running.

Verifies:

* instance-row port wins when present (``source="instance_row"``)
* allocator claim is used when NO row exists (``source="port_allocator"``)
* allocator claim is used when the row exists but its port is null
* a cross-host row's host is preserved (routes to the peer)
* nothing resolved → ``source="none"`` / ``a2a_port=None``
* the ``bound_port`` column is preferred over legacy ``a2a_port``

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. State is
seeded into a REAL temp ``state.db`` via the real ``record_instance_start``
/ ``claim_port`` helpers. STX-TQ007: one fact per test. STX-TQ002: AAA.
"""

from __future__ import annotations

import importlib
import os

import pytest

from scitex_agent_container.cli_pkg._send_resolve import resolve_send_endpoint

_LOCAL_HOST = "lead-host"


@pytest.fixture
def state_db_env(tmp_path, pg_schema: str):
    """Redirect state.db + host to a temp sandbox; reload the module.

    Mirrors ``test__send.py``'s fixture so the resolver reads an empty db
    unless the test seeds rows / claims itself.

    DEPENDS ON ``pg_schema`` since 2026-08-28: the durable allocator claim
    this resolver falls back to lives in PostgreSQL now, so a temp state.db is
    only half the sandbox.
    """
    saved_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_host = os.environ.get("SAC_HOST")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(tmp_path / "state.db")
    os.environ["SAC_HOST"] = _LOCAL_HOST
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield tmp_path
    finally:
        if saved_db is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db
        if saved_host is None:
            os.environ.pop("SAC_HOST", None)
        else:
            os.environ["SAC_HOST"] = saved_host
        importlib.reload(_state_db_mod)


def _seed_row(name: str, *, host: str = _LOCAL_HOST, a2a_port=None, bound_port=None):
    """Insert a real active ``instances`` row."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(
        name=name, host=host, a2a_port=a2a_port, bound_port=bound_port
    )


def _seed_claim(name: str, port: int) -> None:
    """Insert a real durable ``a2a_ports`` allocator claim."""
    from scitex_agent_container._state.port_allocator import claim_port

    claim_port(name, explicit=port)


# ---------------------------------------------------------------------------
# instance-row port wins when present
# ---------------------------------------------------------------------------


def test_resolve_uses_instance_row_port_when_present(state_db_env):
    # Arrange
    _seed_row("alpha", a2a_port=12345)
    # Act
    ep = resolve_send_endpoint("alpha", current_host=_LOCAL_HOST)
    # Assert
    assert ep.a2a_port == 12345


def test_resolve_instance_row_source_label(state_db_env):
    # Arrange
    _seed_row("alpha", a2a_port=12345)
    # Act
    ep = resolve_send_endpoint("alpha", current_host=_LOCAL_HOST)
    # Assert
    assert ep.source == "instance_row"


def test_resolve_prefers_bound_port_over_legacy_a2a_port(state_db_env):
    # Arrange — both columns set to different values; bound_port must win.
    _seed_row("alpha", a2a_port=11111, bound_port=22222)
    # Act
    ep = resolve_send_endpoint("alpha", current_host=_LOCAL_HOST)
    # Assert
    assert ep.a2a_port == 22222


# ---------------------------------------------------------------------------
# the split-brain fix: allocator claim used when the row is stale/absent
# ---------------------------------------------------------------------------


def test_resolve_falls_back_to_allocator_when_no_row(state_db_env):
    # Arrange — NO instances row, but a durable allocator claim exists.
    # This is the health-monitor-restart case: the row was ended but the
    # claim (released only on stop/--force) survives.
    _seed_claim("beta", 19007)
    # Act
    ep = resolve_send_endpoint("beta", current_host=_LOCAL_HOST)
    # Assert
    assert ep.a2a_port == 19007


def test_resolve_allocator_fallback_source_label(state_db_env):
    # Arrange
    _seed_claim("beta", 19007)
    # Act
    ep = resolve_send_endpoint("beta", current_host=_LOCAL_HOST)
    # Assert
    assert ep.source == "port_allocator"


def test_resolve_allocator_fallback_host_is_local(state_db_env):
    # Arrange — an allocator claim is local by construction.
    _seed_claim("beta", 19007)
    # Act
    ep = resolve_send_endpoint("beta", current_host=_LOCAL_HOST)
    # Assert
    assert ep.host == _LOCAL_HOST


def test_resolve_falls_back_to_allocator_when_row_port_is_null(state_db_env):
    # Arrange — a row exists but carries NO port; the claim supplies it.
    _seed_row("beta", a2a_port=None)
    _seed_claim("beta", 19007)
    # Act
    ep = resolve_send_endpoint("beta", current_host=_LOCAL_HOST)
    # Assert
    assert ep.a2a_port == 19007


# ---------------------------------------------------------------------------
# cross-host row host is preserved
# ---------------------------------------------------------------------------


def test_resolve_cross_host_row_preserves_peer_host(state_db_env):
    # Arrange — a remote row (different host) carries the peer + port.
    _seed_row("gamma", host="peer-x", a2a_port=18888)
    # Act
    ep = resolve_send_endpoint("gamma", current_host=_LOCAL_HOST)
    # Assert
    assert ep.host == "peer-x"


# ---------------------------------------------------------------------------
# nothing resolved → source="none"
# ---------------------------------------------------------------------------


def test_resolve_nothing_known_returns_source_none(state_db_env):
    # Arrange — no row, no claim.
    # Act
    ep = resolve_send_endpoint("ghost", current_host=_LOCAL_HOST)
    # Assert
    assert ep.source == "none"


def test_resolve_nothing_known_returns_null_port(state_db_env):
    # Arrange — no row, no claim.
    # Act
    ep = resolve_send_endpoint("ghost", current_host=_LOCAL_HOST)
    # Assert
    assert ep.a2a_port is None
