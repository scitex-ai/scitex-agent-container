"""Tests for ``cli_pkg.lifecycle._host_routing`` (transparent remote routing).

Covers the chain-aware spec-host classifier, the fail-loud unknown-host
message, and the stop/restart spec-host fallback (row-gated). No-mocks:
real specs on a redirected HOME, real state.db writes via the reloaded
module, pure functions exercised directly. Conforms to STX-TQ002 (AAA
markers), STX-TQ003 (descriptive names), STX-TQ007 (one assert per test).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._host_routing import (
    UnknownSpecHostError,
    classify_spec_host_route,
    format_unknown_host_error,
    has_active_row,
    resolve_spec_host_peer,
    spec_host_fallback_peer,
)

# ---------------------------------------------------------------------------
# Fixtures — HOME redirect + isolated state.db (same pattern as
# test__dispatch.py) and a minimal VALID v3 spec written under fake HOME.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, env_save_restore):
    """Redirect HOME so Path.home() returns tmp_path."""
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def state_db(fake_home: Path):
    """Redirect state.db under fake_home (module reload; see test__dispatch)."""
    import importlib
    import os as _os

    db = fake_home / "state.db"
    saved = _os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    _os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield db
    finally:
        if saved is None:
            _os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            _os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved
        importlib.reload(_state_db_mod)


def _write_spec(home: Path, name: str, host_line: str) -> Path:
    """Write a minimal VALID v3 spec pinned by ``host_line`` under fake HOME."""
    d = home / ".scitex" / "agent-container" / "agents" / name
    d.mkdir(parents=True)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        f"  host: {host_line}\n"
        "  workdir: /tmp/hr-test\n"
        "  apptainer:\n"
        "    image: /tmp/does-not-need-to-exist.sif\n"
        "    binds: []\n"
        "  claude:\n"
        "    model: haiku\n"
        "  health:\n"
        "    enabled: true\n"
        "    interval: 60\n"
        "  restart:\n"
        "    policy: on-failure\n"
        "    max_retries: 3\n"
    )
    spec = d / "spec.yaml"
    spec.write_text(body)
    return spec


_PEERS = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}


# ---------------------------------------------------------------------------
# classify_spec_host_route — chain-aware local / remote / unknown.
# ---------------------------------------------------------------------------


def test_classify_empty_host_is_local():
    # Arrange — unset placement means the caller's host.
    spec_host = ""
    # Act
    kind_peer = classify_spec_host_route(
        spec_host, "this-host", _PEERS, local_names={"this-host"}
    )
    # Assert
    assert kind_peer == ("local", None)


def test_classify_current_host_string_is_local():
    # Arrange
    spec_host = "this-host"
    # Act
    kind_peer = classify_spec_host_route(
        spec_host, "this-host", _PEERS, local_names={"this-host"}
    )
    # Assert
    assert kind_peer == ("local", None)


def test_classify_registered_peer_is_remote_with_peer_name():
    # Arrange
    spec_host = "peer-host"
    # Act
    kind_peer = classify_spec_host_route(
        spec_host, "this-host", _PEERS, local_names={"this-host"}
    )
    # Assert
    assert kind_peer == ("remote", "peer-host")


def test_classify_unregistered_host_is_unknown():
    # Arrange
    spec_host = "spartn-typo"
    # Act
    kind_peer = classify_spec_host_route(
        spec_host, "this-host", _PEERS, local_names={"this-host"}
    )
    # Assert
    assert kind_peer == ("unknown", None)


def test_classify_chain_head_peer_is_remote():
    # Arrange — the HEAD of a fallback chain drives remote routing.
    spec_host = ["peer-host", "this-host"]
    # Act
    kind_peer = classify_spec_host_route(
        spec_host, "this-host", _PEERS, local_names={"this-host"}
    )
    # Assert
    assert kind_peer == ("remote", "peer-host")


def test_classify_chain_unknown_head_with_local_tail_is_local():
    # Arrange — dead head, but the tail names THIS machine: the documented
    # fallback-hosts semantics keep the local path (no fail-loud).
    spec_host = ["dead-host", "this-host"]
    # Act
    kind_peer = classify_spec_host_route(
        spec_host, "this-host", _PEERS, local_names={"this-host"}
    )
    # Assert
    assert kind_peer == ("local", None)


def test_classify_chain_unknown_head_without_local_tail_is_unknown():
    # Arrange — nothing in the chain resolves to this machine or a peer.
    spec_host = ["dead-host", "other-dead"]
    # Act
    kind_peer = classify_spec_host_route(
        spec_host, "this-host", _PEERS, local_names={"this-host"}
    )
    # Assert
    assert kind_peer == ("unknown", None)


# ---------------------------------------------------------------------------
# format_unknown_host_error — actionable, names the registered peers.
# ---------------------------------------------------------------------------


def test_unknown_host_error_names_registered_peers():
    # Arrange
    peers = _PEERS
    # Act
    msg = format_unknown_host_error("alpha", "spartn-typo", peers, verb="start")
    # Assert
    assert "peer-host" in msg


def test_unknown_host_error_points_at_sac_host_list():
    # Arrange
    peers = _PEERS
    # Act
    msg = format_unknown_host_error("alpha", "spartn-typo", peers, verb="start")
    # Assert
    assert "sac host list" in msg


def test_unknown_host_error_for_start_offers_no_redispatch_escape():
    # Arrange
    peers = _PEERS
    # Act
    msg = format_unknown_host_error("alpha", "spartn-typo", peers, verb="start")
    # Assert
    assert "--no-redispatch" in msg


def test_unknown_host_error_for_stop_offers_on_peer_escape():
    # Arrange
    peers = _PEERS
    # Act
    msg = format_unknown_host_error("alpha", "spartn-typo", peers, verb="stop")
    # Assert
    assert "sac --on" in msg


def test_unknown_host_error_with_no_peers_says_none_registered():
    # Arrange
    peers: dict = {}
    # Act
    msg = format_unknown_host_error("alpha", "spartn-typo", peers, verb="start")
    # Assert
    assert "(none registered)" in msg


# ---------------------------------------------------------------------------
# has_active_row — row-gate for the spec-host fallback.
# ---------------------------------------------------------------------------


def test_has_active_row_false_on_fresh_state_db(fake_home, state_db):
    # Arrange — no instances written at all.
    name = "hr-alpha"
    # Act
    present = has_active_row(name)
    # Assert
    assert present is False


def test_has_active_row_true_after_record_instance_start(fake_home, state_db):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="hr-alpha", host="peer-host")
    # Act
    present = has_active_row("hr-alpha")
    # Assert
    assert present is True


# ---------------------------------------------------------------------------
# resolve_spec_host_peer — spec-driven verb routing.
# ---------------------------------------------------------------------------


def test_resolve_spec_pinned_to_current_host_returns_none(fake_home):
    # Arrange
    _write_spec(fake_home, "hr-local", "this-host")
    # Act
    peer = resolve_spec_host_peer(
        "hr-local",
        _PEERS,
        verb="stop",
        current_host="this-host",
        local_names={"this-host"},
    )
    # Assert
    assert peer is None


def test_resolve_spec_pinned_to_registered_peer_returns_peer(fake_home):
    # Arrange
    _write_spec(fake_home, "hr-remote", "peer-host")
    # Act
    peer = resolve_spec_host_peer(
        "hr-remote",
        _PEERS,
        verb="stop",
        current_host="this-host",
        local_names={"this-host"},
    )
    # Assert
    assert peer == "peer-host"


def test_resolve_spec_pinned_to_unknown_host_raises(fake_home):
    # Arrange
    _write_spec(fake_home, "hr-typo", "spartn-typo")

    # Act
    def _do() -> None:
        resolve_spec_host_peer(
            "hr-typo",
            _PEERS,
            verb="restart",
            current_host="this-host",
            local_names={"this-host"},
        )

    # Assert
    with pytest.raises(UnknownSpecHostError, match="peer-host"):
        _do()


def test_resolve_unresolvable_agent_name_returns_none(fake_home):
    # Arrange — no spec anywhere for this name; the local verb path owns
    # the "not found" error surface, so the router yields None.
    name = "hr-zzz-does-not-exist"
    # Act
    peer = resolve_spec_host_peer(
        name,
        _PEERS,
        verb="stop",
        current_host="this-host",
        local_names={"this-host"},
    )
    # Assert
    assert peer is None


def test_resolve_spec_with_hostname_placeholder_is_local(fake_home, env_save_restore):
    # Arrange — ${HOSTNAME} placement resolves to THIS machine at load time,
    # so routing classifies local. Both env forms are set so the override
    # never conflicts with a pre-set SAC_HOSTNAME in the runner env.
    env_save_restore.set("SAC_HOSTNAME", "this-host")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
    _write_spec(fake_home, "hr-portable", "${HOSTNAME}")
    # Act
    peer = resolve_spec_host_peer(
        "hr-portable",
        _PEERS,
        verb="stop",
        current_host="this-host",
        local_names={"this-host"},
    )
    # Assert
    assert peer is None


# ---------------------------------------------------------------------------
# spec_host_fallback_peer — row-gated composition.
# ---------------------------------------------------------------------------


def test_fallback_returns_none_when_an_active_row_exists(fake_home, state_db):
    # Arrange — a live row (even remote) means the row-driven dispatcher's
    # answer stands; the spec fallback must not engage.
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="hr-rowed", host="peer-host")
    # Act
    peer = spec_host_fallback_peer("hr-rowed", _PEERS, verb="stop")
    # Assert
    assert peer is None


def test_fallback_routes_by_spec_pin_when_no_row_exists(
    fake_home, state_db, env_save_restore
):
    # Arrange — no row; the spec pins a registered peer. The env override
    # (both forms, so no SacEnvConflict with a pre-set SAC_HOSTNAME) keeps
    # current-host resolution hermetic on any machine.
    env_save_restore.set("SAC_HOSTNAME", "this-host")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "this-host")
    _write_spec(fake_home, "hr-fallback", "peer-host")
    # Act
    peer = spec_host_fallback_peer("hr-fallback", _PEERS, verb="restart")
    # Assert
    assert peer == "peer-host"
