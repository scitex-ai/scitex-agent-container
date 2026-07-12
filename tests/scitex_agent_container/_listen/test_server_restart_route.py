"""Wired tests for the POST /agents/<name>/restart route ACL gate.

The container-side restart bypass (mirror of the spawn bypass) routes
through the real Starlette ``TestClient`` + a per-node bearer mapping,
exactly like :mod:`test_server_lineage_acl` does for DELETE / tail:

  * a non-host-bearer caller with no lineage edge AND no group mesh to
    ``<name>`` lands on 403 + ``kind="acl_deny"``;
  * a host-bearer caller (admin) is NOT blocked by the gate (it shells
    the bare-host restart, which fails to resolve the ghost agent — but
    the ACL gate is not the failure cause).

These assert the gate is wired on the new route; the host-shell leg
(``sac agents restart``) is exercised separately by the CLI tests.

No mocks (PA-306); AAA + one assert (PA-307). Node tokens seeded via
:func:`mint_node_token` (the real persistence path the server reads).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen import _agent_restart as restart_handler_mod
from scitex_agent_container._listen._agent_restart import _build_detached_restart_argv
from scitex_agent_container._listen.server import create_app
from scitex_agent_container._state.state_db_nodes import (
    mint_node_token,
    record_comms_policy,
)

HOST_TOKEN = "test-host-bearer"


@pytest.fixture
def isolated_env(tmp_path: Path, env_save_restore):
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    yaml_dir = home / ".scitex" / "agent-container" / "agents"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    state_db_path = tmp_path / "state.db"
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(runtime))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_dir))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(state_db_path))
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    yield tmp_path
    os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
    os.environ.pop("HOME", None)
    importlib.reload(ss)


@pytest.fixture
def client(isolated_env):
    app = create_app(token=HOST_TOKEN)
    with TestClient(app) as c:
        yield c


def _node_headers(name: str) -> dict[str, str]:
    token = mint_node_token(name=name)
    return {"authorization": f"Bearer {token}"}


def _host_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {HOST_TOKEN}"}


# ---------------------------------------------------------------------------
# restart — non-host bearer w/ no lineage / no mesh → 403 acl_deny
# ---------------------------------------------------------------------------


def test_restart_unrelated_caller_returns_403(client, isolated_env):
    # Arrange — alice has no lineage edge and no group mesh to the target.
    headers = _node_headers("alice")
    # Act
    response = client.post("/agents/unrelated-target/restart", headers=headers)
    # Assert
    assert response.status_code == 403


def test_restart_unrelated_caller_body_has_kind_acl_deny(client, isolated_env):
    # Arrange
    headers = _node_headers("alice")
    # Act
    response = client.post("/agents/unrelated-target-2/restart", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert body["kind"] == "acl_deny"


# ---------------------------------------------------------------------------
# restart — host bearer (admin) is NOT blocked by the ACL gate
# ---------------------------------------------------------------------------


def test_restart_with_host_bearer_does_not_403(client, isolated_env):
    # Arrange — host bearer is the admin path; the gate must allow even
    # though the ghost agent has no row (the bare-host shell will fail,
    # but NOT with a 403 from the ACL gate).
    # Act
    response = client.post("/agents/ghost/restart", headers=_host_headers())
    # Assert
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# restart — standard-fleet mesh: a researcher may manage a developer peer
# ---------------------------------------------------------------------------


def test_restart_researcher_to_developer_not_403(client, isolated_env):
    # Arrange — neurovista (researcher) restarts scitex-todo (developer):
    # the manage mesh allows it with no lineage edge. The target has no
    # row so the bare-host shell fails, but NOT via a 403 ACL deny.
    record_comms_policy(name="neurovista", group_name="researcher")
    record_comms_policy(name="scitex-todo", group_name="developer")
    headers = _node_headers("neurovista")
    # Act
    response = client.post("/agents/scitex-todo/restart", headers=headers)
    # Assert
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# _build_detached_restart_argv — PURE builder (no spawn, no mocks, PA-306).
# The self-restart argv is asserted here directly, per the "structure the
# code so the argv is assert-able" contract — never by bouncing a real agent.
# ---------------------------------------------------------------------------

_SAC = "/opt/venv-sac/bin/sac"
_LOG = "/run/agent-x/self-restart.log"


def test_build_detached_argv_starts_with_setsid():
    # Arrange — non-fresh (resume) bounce for agent-x.
    name = "agent-x"
    # Act
    argv = _build_detached_restart_argv(_SAC, name, fresh=False, delay_s=3, log_path=_LOG)
    # Assert — detachment is via setsid (child survives the caller's death).
    assert argv[0] == "setsid"


def test_build_detached_argv_is_setsid_sh_dash_c():
    # Arrange
    name = "agent-x"
    # Act
    argv = _build_detached_restart_argv(_SAC, name, fresh=False, delay_s=3, log_path=_LOG)
    # Assert — the deferred command is carried as a single sh -c program.
    assert argv[:3] == ["setsid", "sh", "-c"]


def test_build_detached_argv_nonfresh_forces_start_without_fresh():
    # Arrange — non-fresh (resume) bounce.
    name = "agent-x"
    # Act
    argv = _build_detached_restart_argv(_SAC, name, fresh=False, delay_s=3, log_path=_LOG)
    # Assert — `agents start --force` (spec-policy session == plain restart),
    # NO --fresh: the resuming restart is preserved.
    assert "agents start agent-x --force --json" in argv[-1] and "--fresh" not in argv[-1]


def test_build_detached_argv_fresh_appends_fresh_flag():
    # Arrange — fresh (no-resume) bounce.
    name = "agent-x"
    # Act
    argv = _build_detached_restart_argv(_SAC, name, fresh=True, delay_s=3, log_path=_LOG)
    # Assert
    assert "agents start agent-x --force --fresh --json" in argv[-1]


def test_build_detached_argv_defers_by_the_delay():
    # Arrange
    name = "agent-x"
    # Act
    argv = _build_detached_restart_argv(_SAC, name, fresh=False, delay_s=5, log_path=_LOG)
    # Assert — sleeps FIRST so THIS handler's 202 flushes to the caller.
    assert argv[-1].startswith("sleep 5;")


def test_build_detached_argv_logs_to_file_not_devnull():
    # Arrange
    name = "agent-x"
    # Act
    argv = _build_detached_restart_argv(_SAC, name, fresh=False, delay_s=3, log_path=_LOG)
    # Assert — post-hoc debuggable: appended to the log, never /dev/null.
    assert _LOG in argv[-1] and "/dev/null" not in argv[-1]


def test_build_detached_argv_names_the_agent_in_the_bounce():
    # Arrange
    name = "agent-x"
    # Act
    argv = _build_detached_restart_argv(_SAC, name, fresh=False, delay_s=3, log_path=_LOG)
    # Assert — the forced bounce targets exactly this agent.
    assert "start agent-x --force" in argv[-1]


# ---------------------------------------------------------------------------
# Self-restart handler wiring — caller IS the target → detached 202, no
# deadlock. The one OS-spawn seam is swapped (hand-rolled save/restore, the
# same idiom as test__restart.py's `_swap`) so no real bouncer forks; the
# argv is asserted from the recorder AND (independently) above.
# ---------------------------------------------------------------------------


class _SpawnRecorder:
    """Records ``_spawn_detached(argv, *, env)`` calls WITHOUT forking a real
    process (a real detached bouncer would outlive the test and act on the
    live system). Signature-compatible with the seam it replaces.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, *, env):
        self.calls.append((list(argv), dict(env)))


@contextmanager
def _swap(name: str, value) -> Iterator[None]:
    """Replace ``_agent_restart.<name>`` for the block (PA-306: hand-rolled
    seam, no MagicMock / no monkeypatch)."""
    saved = getattr(restart_handler_mod, name)
    setattr(restart_handler_mod, name, value)
    try:
        yield
    finally:
        setattr(restart_handler_mod, name, saved)


def test_self_restart_returns_202(client, isolated_env):
    # Arrange — alice restarts alice (caller == target). Swap sac_binary (so
    # the test is hermetic w.r.t. the install layout) + the spawn seam.
    recorder = _SpawnRecorder()
    headers = _node_headers("alice")
    # Act
    with _swap("sac_binary", lambda: "/fake/sac"), _swap("_spawn_detached", recorder):
        response = client.post("/agents/alice/restart", headers=headers)
    # Assert — clean 202, NOT the confusing 502 the sync path produced.
    assert response.status_code == 202


def test_self_restart_body_marks_scheduled(client, isolated_env):
    # Arrange
    recorder = _SpawnRecorder()
    headers = _node_headers("alice")
    # Act
    with _swap("sac_binary", lambda: "/fake/sac"), _swap("_spawn_detached", recorder):
        response = client.post("/agents/alice/restart", headers=headers)
    body = json.loads(response.content)
    # Assert
    assert body["self_restart"] == "scheduled"


def test_self_restart_spawns_detached_setsid_bounce(client, isolated_env):
    # Arrange
    recorder = _SpawnRecorder()
    headers = _node_headers("alice")
    # Act
    with _swap("sac_binary", lambda: "/fake/sac"), _swap("_spawn_detached", recorder):
        client.post("/agents/alice/restart", headers=headers)
    argv = recorder.calls[0][0]
    # Assert — a detached (setsid) forced bounce naming the agent was spawned.
    assert argv[0] == "setsid" and "agents start alice --force --json" in argv[-1]


def test_self_restart_fresh_bounce_carries_fresh_flag(client, isolated_env):
    # Arrange — fresh=true self-restart → detached bounce carries --fresh.
    recorder = _SpawnRecorder()
    headers = _node_headers("alice")
    # Act
    with _swap("sac_binary", lambda: "/fake/sac"), _swap("_spawn_detached", recorder):
        client.post("/agents/alice/restart", headers=headers, json={"fresh": True})
    # Assert
    assert "agents start alice --force --fresh --json" in recorder.calls[0][0][-1]


def test_self_restart_bounce_env_strips_apptainer_marker(client, isolated_env):
    # Arrange — the detached bounce must inherit the in-SIF-stripped env so it
    # never re-brokers back into a container (same recursion guard as sync).
    recorder = _SpawnRecorder()
    headers = _node_headers("alice")
    os.environ["APPTAINER_CONTAINER"] = "/some/parent.sif"
    try:
        # Act
        with _swap("sac_binary", lambda: "/fake/sac"), _swap(
            "_spawn_detached", recorder
        ):
            client.post("/agents/alice/restart", headers=headers)
    finally:
        os.environ.pop("APPTAINER_CONTAINER", None)
    # Assert
    assert "APPTAINER_CONTAINER" not in recorder.calls[0][1]


# ---------------------------------------------------------------------------
# External / admin restart (caller != target) — the synchronous path is
# UNCHANGED: the self-restart branch is skipped, NO detached bounce spawns.
# ---------------------------------------------------------------------------


def test_admin_restart_caller_none_does_not_self_schedule(client, isolated_env):
    # Arrange — host bearer resolves caller to None (admin); target is a ghost.
    # Only the spawn seam is swapped (sac_binary is left real so the sync
    # bare-host shell runs exactly as before); the recorder proves the
    # self-restart branch was NOT taken.
    recorder = _SpawnRecorder()
    # Act
    with _swap("_spawn_detached", recorder):
        response = client.post("/agents/ghost/restart", headers=_host_headers())
    body = json.loads(response.content)
    # Assert — synchronous path: no detached bounce, no self_restart marker.
    assert recorder.calls == [] and "self_restart" not in body


def test_node_caller_restarting_peer_does_not_self_schedule(client, isolated_env):
    # Arrange — neurovista restarts scitex-todo (mesh-allowed, caller != name):
    # the self-restart branch must be skipped, sync path taken.
    record_comms_policy(name="neurovista", group_name="researcher")
    record_comms_policy(name="scitex-todo", group_name="developer")
    recorder = _SpawnRecorder()
    headers = _node_headers("neurovista")
    # Act
    with _swap("_spawn_detached", recorder):
        response = client.post("/agents/scitex-todo/restart", headers=headers)
    # Assert — an allowed cross-agent restart never self-schedules.
    assert recorder.calls == [] and response.status_code != 403
