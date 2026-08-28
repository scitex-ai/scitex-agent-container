"""Tests for ``_mcp/_channel_self_register.py`` — channel-side comms_nodes UPSERT.

ADR-0014 + the lead-registration bug: the lead's comms_nodes row had
``a2a_port=0`` and an ``updated_at`` that never refreshed because
nothing on the ``sac mcp channel`` side called ``register_comms_node``.
This module is the missing piece — it registers the channel process
(lead OR any other node consuming SSE from a sac listen) into
comms_nodes on startup, then refreshes ``updated_at`` on a 10s
cadence matching the agent runner's heartbeat.

Real on-disk state.db + config.yaml via the shared ``env_save_restore``
fixture; no mocks. AAA, ≥3-word test names, one assert per test
(STX-TQ002 / PA-307).
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/.../cli_pkg/test_listen_cmds_comms_nodes.py so a
# future audit sees the same shape across both registration sites.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path, env_save_restore):
    p = tmp_path / "state.db"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(p))
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    yield p
    importlib.reload(mod)


@pytest.fixture
def cfg_with_lead(tmp_path: Path, env_save_restore) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "host": {"canonical": "lead-host"},
                "lead": {"name": "lead", "host": "lead-host", "a2a_port": 7878},
                "peers": {},
            }
        )
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# parse_listen_port — string → int extraction
# ---------------------------------------------------------------------------


def test_parse_listen_port_extracts_port_from_full_http_url() -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_self_register import parse_listen_port

    # Act
    port = parse_listen_port("http://127.0.0.1:7878")
    # Assert
    assert port == 7878


def test_parse_listen_port_returns_none_for_empty_string() -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_self_register import parse_listen_port

    # Act
    port = parse_listen_port("")
    # Assert
    assert port is None


def test_parse_listen_port_returns_none_for_portless_url() -> None:
    # Arrange — no port in the URL means we don't know what to advertise.
    from scitex_agent_container._mcp._channel_self_register import parse_listen_port

    # Act
    port = parse_listen_port("http://lead-host/")
    # Assert
    assert port is None


def test_parse_listen_port_returns_none_for_url_with_port_zero() -> None:
    # Arrange — port=0 is the EXACT production-bug signature we are
    # explicitly guarding against; refuse to extract it.
    from scitex_agent_container._mcp._channel_self_register import parse_listen_port

    # Act
    port = parse_listen_port("http://127.0.0.1:0/")
    # Assert
    assert port is None


def test_parse_listen_port_handles_https_scheme() -> None:
    # Arrange — sac listen can run behind tls (tunnel pattern).
    from scitex_agent_container._mcp._channel_self_register import parse_listen_port

    # Act
    port = parse_listen_port("https://lead-host:443/")
    # Assert
    assert port == 443


# ---------------------------------------------------------------------------
# register_self_node — happy path
# ---------------------------------------------------------------------------


def test_register_self_node_writes_comms_nodes_row_for_lead_name(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info is not None


def test_register_self_node_records_port_parsed_from_listen_url(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — the production bug stored a2a_port=0 because nothing
    # parsed the URL; pin the correct extracted port here.
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["a2a_port"] == 7878


def test_register_self_node_uses_canonical_host_from_config(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — cfg has host.canonical=lead-host; that's what the row
    # should advertise as the dialable host, NOT 127.0.0.1 from the URL.
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["host"] == "lead-host"


def test_register_self_node_source_host_is_none_for_local_registration(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — locally-registered rows have source_host=None (the sync
    # contract distinguishes self-registrations from peer-pulled rows).
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Assert
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    info = lookup_comms_node(name="lead")
    assert info["source_host"] is None


def test_register_self_node_returns_true_on_successful_write(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    ok = register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Assert
    assert ok is True


def test_register_self_node_is_idempotent_for_same_name(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — second call must UPDATE not INSERT (no DUPLICATE PK).
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Act
    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Assert
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes

    rows = [r for r in list_comms_nodes() if r["name"] == "lead"]
    assert len(rows) == 1


def test_register_self_node_refresh_advances_updated_at(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — second call must bump updated_at so a stale-row
    # detector sees the row as fresh. Without this the production bug
    # surfaces (updated_at frozen at registered_at).
    import time

    from scitex_agent_container._mcp._channel_self_register import register_self_node
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    first_updated_at = lookup_comms_node(name="lead")["updated_at"]
    time.sleep(0.01)  # advance the clock just enough to make a delta visible
    # Act
    register_self_node(name="lead", listen_url="http://127.0.0.1:7878")
    # Assert
    second_updated_at = lookup_comms_node(name="lead")["updated_at"]
    assert second_updated_at > first_updated_at


# ---------------------------------------------------------------------------
# register_self_node — refuse-to-write guards (the production bug)
# ---------------------------------------------------------------------------


def test_register_self_node_writes_no_row_when_listen_url_has_port_zero(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — the EXACT production-bug signature was port=0. The
    # function MUST refuse rather than persist a 0 port (which is what
    # broke `sac a2a peers` resolution in the first place).
    from scitex_agent_container._mcp._channel_self_register import register_self_node
    from scitex_agent_container._state.state_db_nodes import list_comms_nodes

    # Act
    register_self_node(name="lead", listen_url="http://127.0.0.1:0/")
    # Assert — no row was written.
    assert list_comms_nodes() == []


def test_register_self_node_returns_false_for_portless_url(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    ok = register_self_node(name="lead", listen_url="http://lead-host/")
    # Assert
    assert ok is False


def test_register_self_node_does_not_raise_when_name_is_empty(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — best-effort contract: callers must not have to wrap
    # this in try/except. An empty name is logged + skipped.
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    ok = register_self_node(name="", listen_url="http://127.0.0.1:7878")
    # Assert
    assert ok is False


def test_register_self_node_does_not_raise_when_listen_url_is_empty(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange
    from scitex_agent_container._mcp._channel_self_register import register_self_node

    # Act
    ok = register_self_node(name="lead", listen_url="")
    # Assert
    assert ok is False


# ---------------------------------------------------------------------------
# refresh_node — the async heartbeat loop
# ---------------------------------------------------------------------------


def test_refresh_node_writes_initial_row_on_first_tick(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — refresh_node should write a row on its first iteration
    # so a caller doesn't have to call register_self_node separately
    # before kicking off the loop.
    from scitex_agent_container._mcp._channel_self_register import refresh_node
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    async def _drive_one_tick() -> None:
        task = asyncio.create_task(
            refresh_node(name="lead", listen_url="http://127.0.0.1:7878", interval_s=10)
        )
        await asyncio.sleep(0.05)  # give the loop a chance to register once
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Act
    asyncio.run(_drive_one_tick())
    # Assert
    info = lookup_comms_node(name="lead")
    assert info is not None


def test_refresh_node_advances_updated_at_across_ticks(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — let the loop tick twice; updated_at must advance.
    from scitex_agent_container._mcp._channel_self_register import refresh_node
    from scitex_agent_container._state.state_db_nodes import lookup_comms_node

    captured: dict[str, float] = {}

    async def _drive_two_ticks() -> None:
        task = asyncio.create_task(
            refresh_node(
                name="lead",
                listen_url="http://127.0.0.1:7878",
                interval_s=0.01,
            )
        )
        await asyncio.sleep(0.005)  # ~first tick registered
        captured["first"] = lookup_comms_node(name="lead")["updated_at"]
        await asyncio.sleep(0.05)  # let several more ticks fire
        captured["last"] = lookup_comms_node(name="lead")["updated_at"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Act
    asyncio.run(_drive_two_ticks())
    # Assert
    assert captured["last"] > captured["first"]


def test_refresh_node_respects_cancellation_promptly(
    db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — the loop must propagate CancelledError so the channel's
    # shutdown path can tear it down cleanly (no orphan task).
    from scitex_agent_container._mcp._channel_self_register import refresh_node

    async def _start_then_cancel() -> bool:
        task = asyncio.create_task(
            refresh_node(name="lead", listen_url="http://127.0.0.1:7878", interval_s=5)
        )
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    # Act
    cancelled = asyncio.run(_start_then_cancel())
    # Assert
    assert cancelled is True


# ---------------------------------------------------------------------------
# End-to-end — channel-start → resolve_node_host('lead') succeeds
#
# The lead's request (msg a26da20, 2026-06-05): pin the contract that
# after the channel's refresh task fires its first iteration, the
# resolver every cross-host A2A POST uses (resolve_node_host, not just
# lookup_comms_node) returns the right row. Without this, a future
# regression that breaks the channel→resolver seam (e.g. someone
# decoupling the registration target from the resolver) would land
# silently — the lower-level lookup_comms_node tests above would still
# pass while production cross-host routing broke.
# ---------------------------------------------------------------------------


def test_refresh_node_makes_lead_resolvable_via_resolve_node_host(
    pg_schema: str, db_path: Path, cfg_with_lead: Path
) -> None:
    # Arrange — drive one refresh tick (the same path channel.py
    # _serve() schedules at startup) then ask the production resolver.
    from scitex_agent_container._mcp._channel_self_register import refresh_node
    from scitex_agent_container._state.state_db_nodes import resolve_node_host

    async def _drive_one_tick() -> None:
        task = asyncio.create_task(
            refresh_node(name="lead", listen_url="http://127.0.0.1:7878", interval_s=10)
        )
        await asyncio.sleep(
            0.05
        )  # one tick is enough; first iteration runs immediately
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Act
    asyncio.run(_drive_one_tick())
    info = resolve_node_host(name="lead")
    # Assert — the full {host, a2a_port} dict, from canonical_host + the
    # parsed listen_url port. host comes from cfg_with_lead's "lead-host".
    assert info == {"host": "lead-host", "a2a_port": 7878}
