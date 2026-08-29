"""Tests for fail-loud peer-URL resolution (#192).

The pre-#192 resolver silently assumed an agent was local when its
``spec.host`` was empty and a port happened to resolve, even when the
cross-host registry placed it on a different host. It also raised an
uninformative "is the agent running?" with no last-known-host evidence.

These tests prove the resolver now:
  * RAISES an informative ``PeerError`` (naming the last-known host +
    timestamp) when no live instance resolves — never silently local.
  * RAISES when a stale local resolution contradicts a fresh
    ``remote=True`` instances row — refusing to send to the wrong endpoint.

No-mocks: real on-disk isolated state.db rows + real YAML files swapped
through ``resolve_config``. Conforms to STX-TQ002 (AAA markers), STX-TQ003
(descriptive names), STX-TQ007 (one assertion per test).
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

import importlib
import os
from pathlib import Path

import pytest

from scitex_agent_container._network.peer import PeerError, resolve_peer_url


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield


@pytest.fixture
def resolve_yaml_to():
    """Yield a setter that swaps ``resolve_config`` and restores on teardown."""
    from scitex_agent_container.config import _resolve

    saved = _resolve.resolve_config

    def _set(yaml_path):
        _resolve.resolve_config = lambda _name: str(yaml_path)

    try:
        yield _set
    finally:
        _resolve.resolve_config = saved


@pytest.fixture
def isolated_state_db(tmp_path: Path):
    """Redirect state.db to a tmp path; reload the module so the
    module-level DEFAULT_DB_PATH picks it up (explicit save/restore)."""
    db = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(db)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield db
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _write_auto_port_yaml(tmp_path: Path) -> Path:
    """A v3 YAML with ``a2a.port: auto`` and no ``spec.host`` — clew's shape."""
    y = tmp_path / "clew" / "clew.yaml"
    y.parent.mkdir(parents=True)
    y.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  a2a: {port: auto}\n")
    )
    return y


def _write_static_local_yaml(tmp_path: Path) -> Path:
    """A v3 YAML with a static loopback a2a port and no spec.host."""
    y = tmp_path / "clew" / "clew.yaml"
    y.parent.mkdir(parents=True)
    y.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  a2a: {port: 18888, host: 127.0.0.1}\n")
    )
    return y


# ---------------------------------------------------------------------------
# Unresolvable instance → informative fail-loud (never silent-local)
# ---------------------------------------------------------------------------


class TestUnresolvableInstanceFailsLoud:
    def test_no_live_instance_with_history_names_last_known_host(
        self, tmp_path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — auto-port YAML, no local allocator claim, and an
        # ENDED prior instance row recording the last-known host.
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_auto_port_yaml(tmp_path))
        from scitex_agent_container._state.state_db import (
            record_instance_start,
            record_instance_stop,
        )

        iid = record_instance_start(
            name="clew", host="spartan-bm043", bound_port=19000, remote=True
        )
        record_instance_stop(iid, exit_reason="superseded")
        # Act
        ctx = pytest.raises(PeerError, match="spartan-bm043")
        # Assert — the error names the last-known host.
        with ctx:
            resolve_peer_url("clew")

    def test_no_live_instance_refuses_to_assume_local(
        self, tmp_path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — same ended-history shape.
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_auto_port_yaml(tmp_path))
        from scitex_agent_container._state.state_db import (
            record_instance_start,
            record_instance_stop,
        )

        iid = record_instance_start(
            name="clew", host="spartan-bm043", bound_port=19000, remote=True
        )
        record_instance_stop(iid, exit_reason="superseded")
        # Act
        ctx = pytest.raises(PeerError, match="refusing to assume local")
        # Assert — the message explicitly states the locality refusal.
        with ctx:
            resolve_peer_url("clew")

    def test_no_registry_history_at_all_still_raises_loud(
        self, tmp_path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — auto-port YAML, no allocator claim, NO instances row.
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_auto_port_yaml(tmp_path))
        # Act
        ctx = pytest.raises(PeerError, match="never run on a host this lead knows")
        # Assert
        with ctx:
            resolve_peer_url("clew")


# ---------------------------------------------------------------------------
# Stale-local-vs-fresh-remote contradiction → fail-loud
# ---------------------------------------------------------------------------


class TestStaleLocalContradictsRemoteFailsLoud:
    def test_static_local_port_with_fresh_remote_row_raises(
        self, tmp_path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — a STATIC loopback port in the YAML would resolve
        # local, but the cross-host registry holds a FRESH remote=True
        # row on another host (the #192 unbreakable wrong state).
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_static_local_yaml(tmp_path))
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(
            name="clew", host="spartan-bm001", bound_port=19500, remote=True
        )
        # Act
        ctx = pytest.raises(PeerError, match="stale local endpoint")
        # Assert — refuses the local URL, naming the holding host.
        with ctx:
            resolve_peer_url("clew")

    def test_contradiction_error_names_the_holding_host(
        self, tmp_path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_static_local_yaml(tmp_path))
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(
            name="clew", host="spartan-bm001", bound_port=19500, remote=True
        )
        # Act
        ctx = pytest.raises(PeerError, match="spartan-bm001")
        # Assert
        with ctx:
            resolve_peer_url("clew")

    def test_static_local_port_without_remote_row_resolves_local(
        self, tmp_path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — a static local port and NO contradicting remote row
        # must still resolve to the loopback URL (no false-positive raise).
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_static_local_yaml(tmp_path))
        # Act
        url = resolve_peer_url("clew")
        # Assert
        assert url == "http://127.0.0.1:18888/v1/turn"

    def test_local_row_does_not_contradict_local_resolution(
        self, tmp_path, resolve_yaml_to, isolated_state_db, env_save_restore
    ) -> None:
        # Arrange — a local (remote=False) instances row must NOT trigger
        # the contradiction guard.
        env_save_restore.set("SAC_HOST", "lead-host")
        resolve_yaml_to(_write_static_local_yaml(tmp_path))
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(
            name="clew", host="lead-host", bound_port=18888, remote=False
        )
        # Act
        url = resolve_peer_url("clew")
        # Assert
        assert url == "http://127.0.0.1:18888/v1/turn"
