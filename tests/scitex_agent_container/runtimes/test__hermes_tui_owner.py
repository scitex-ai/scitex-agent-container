from __future__ import annotations

import json

import pytest

from scitex_agent_container.runtimes import _hermes_tui_owner as owner
from scitex_agent_container.runtimes._hermes_tui_rpc import HermesTuiRpcError


class _Process:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def test_old_owner_cleanup_cannot_remove_new_gateway_projection(tmp_path):
    # Arrange: model two rapid owners that both reached readiness.  The newer
    # publication replaces the older generation before the older finally runs.
    owner._publish_gateway_state(
        tmp_path, generation="old-generation", port=40781, gateway_pid=101
    )
    owner._publish_gateway_state(
        tmp_path, generation="new-generation", port=40789, gateway_pid=202
    )

    # Act
    owner._remove_owned_gateway_state(tmp_path, generation="old-generation")

    # Assert: descriptor and ready marker still agree on the live new owner.
    descriptor = json.loads((tmp_path / owner.GATEWAY_FILE).read_text())
    ready = json.loads((tmp_path / owner.READY_FILE).read_text())
    assert descriptor == ready == {
        "generation": "new-generation",
        "owner_pid": descriptor["owner_pid"],
        "pid": 202,
        "port": 40789,
    }


def test_current_owner_cleanup_removes_its_gateway_projection(tmp_path):
    # Arrange
    owner._publish_gateway_state(
        tmp_path, generation="current-generation", port=40789, gateway_pid=202
    )

    # Act
    owner._remove_owned_gateway_state(tmp_path, generation="current-generation")

    # Assert
    assert (
        (tmp_path / owner.GATEWAY_FILE).exists(),
        (tmp_path / owner.READY_FILE).exists(),
    ) == (False, False)


def test_gateway_owner_waits_for_authenticated_readiness():
    # Arrange
    process = _Process()
    observations = []
    responses = iter((None, {"status": "ok", "readiness": {"checks": []}}))

    def detailed(port, token, *, timeout_s):
        observations.append((port, token, timeout_s))
        response = next(responses)
        if response is None:
            raise HermesTuiRpcError("degraded")
        return response

    # Act
    ticks = iter((0.0, 0.0, 0.1, 0.2))
    payload = owner._wait_for_readiness(
        43123,
        "secret-token-1234",
        process,
        detailed_health=detailed,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
    )

    # Assert
    assert (payload["status"], observations) == (
        "ok",
        [(43123, "secret-token-1234", 1.0)] * 2,
    )


def test_gateway_owner_consumes_seed_before_tui_and_deletes_after_import(tmp_path):
    # Arrange
    seed = {
        "version": 1,
        "title": "sac:child:engine-a",
        "parent_session_id": "parent-stored",
        "cwd": "/work/repo",
        "messages": [{"role": "user", "text": "parent nonce"}],
    }
    seed_path = tmp_path / "hermes-fork-seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    seed_path.chmod(0o600)
    imported = []

    def import_(state_dir, payload):
        imported.append((state_dir, payload))
        return "child-stored"

    # Act
    consumed = owner._consume_fork_seed(
        tmp_path,
        [
            "hermes",
            "chat",
            "--tui",
            "--continue",
            "sac:child:engine-a",
            "--create-if-missing",
        ],
        import_fn=import_,
    )
    # Assert
    assert (consumed, imported, seed_path.exists()) == (
        True,
        [(tmp_path, seed)],
        False,
    )


def test_gateway_owner_keeps_seed_when_native_import_fails(tmp_path):
    # Arrange
    seed = {
        "version": 1,
        "title": "sac:child:engine-a",
        "parent_session_id": "parent-stored",
        "cwd": "/work/repo",
        "messages": [{"role": "user", "text": "parent nonce"}],
    }
    seed_path = tmp_path / "hermes-fork-seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    seed_path.chmod(0o600)

    def fail_import(_state_dir, _payload):
        raise HermesTuiRpcError("native import failed")

    # Act / Assert
    with pytest.raises(HermesTuiRpcError, match="native import failed"):
        owner._consume_fork_seed(
            tmp_path,
            ["hermes", "chat", "--continue", "sac:child:engine-a"],
            import_fn=fail_import,
        )
    assert seed_path.is_file()


def test_resume_command_keeps_context_but_never_replays_startup_query():
    # Arrange
    command = [
        "hermes",
        "chat",
        "--tui",
        "--continue",
        "sac:ui",
        "--create-if-missing",
        "--query",
        "initial task",
    ]

    # Act
    resumed = owner._resume_command(command, "stored-123")

    # Assert
    assert resumed == [
        "hermes",
        "chat",
        "--tui",
        "--resume",
        "stored-123",
    ]


def test_fresh_owner_adopts_its_only_isolated_gateway_session():
    # Arrange
    sessions = [{"id": "fresh-1", "title": "generated"}]
    # Act
    selected = owner._select_owned_session(
        sessions, expected_identity="", previous="", adopt_single=True
    )
    # Assert
    assert selected == sessions[0]


def test_resume_request_uses_the_explicit_session_as_owner_identity():
    # Arrange
    command = ["hermes", "chat", "--tui", "--resume", "session-42"]
    # Act
    request = owner._requested_session(command)
    # Assert
    assert request == ("resume", "session-42")


def test_supervisor_reconnects_official_tui_to_exact_persisted_session(tmp_path):
    # Arrange
    # The session is first observed live, then disappears after the
    # gateway orphan reap.  The next TUI generation resumes the exact key and
    # sees it live again.  No pane/spinner text participates in this decision.
    gateway = _Process()
    spawned = []

    def spawn(command, *, env):
        process = _Process()
        spawned.append((list(command), env, process))
        return process

    snapshots = iter(
        (
            [{"id": "runtime-1", "title": "sac:ui", "session_key": "stored-123"}],
            [],
            [],
            [{"id": "runtime-2", "title": "sac:ui", "session_key": "stored-123"}],
        )
    )

    def active_list(_state_dir):
        try:
            rows = next(snapshots)
        except StopIteration:
            spawned[-1][2].returncode = 0
            return [{"session_key": "stored-123"}]
        return rows

    ticks = iter(float(value) for value in range(20))
    command = [
        "hermes",
        "chat",
        "--tui",
        "--continue",
        "sac:ui",
        "--create-if-missing",
        "--query",
        "initial task",
    ]

    # Act
    process, result = owner._supervise_tui(
        command,
        env={"HERMES_HOME": "/profile"},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=active_list,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    assert (
        result,
        process is spawned[-1][2],
        len(spawned),
        spawned[0][2].terminated,
        spawned[1][0][-2:],
        "--query" in spawned[1][0],
    ) == (0, True, 2, True, ["--resume", "stored-123"], False)


def test_observation_failure_never_causes_a_destructive_restart(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []

    def spawn(command, *, env):
        del command, env
        process = _Process()
        spawned.append(process)
        return process

    calls = 0

    def unavailable(_state_dir):
        nonlocal calls
        calls += 1
        if calls == 3:
            spawned[0].returncode = 0
        raise RuntimeError("temporary RPC failure")

    # Act
    _process, result = owner._supervise_tui(
        ["hermes", "chat", "--tui", "--continue", "sac:ui"],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=unavailable,
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    assert (result, len(spawned), spawned[0].terminated) == (0, 1, False)


def test_never_observed_session_is_reconciled_without_replaying_startup_turn(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []

    def spawn(command, *, env):
        process = _Process()
        spawned.append((list(command), env, process))
        return process

    snapshots = iter(([], [], [{"id": "live-1", "title": "sac:ui"}]))

    def active_list(_state_dir):
        try:
            return next(snapshots)
        except StopIteration:
            spawned[-1][2].returncode = 0
            return [{"id": "live-1", "title": "sac:ui"}]

    # Act
    process, result = owner._supervise_tui(
        [
            "hermes",
            "chat",
            "--tui",
            "--continue",
            "sac:ui",
            "--create-if-missing",
            "--query",
            "initial task",
        ],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=active_list,
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    assert (
        result,
        process is spawned[-1][2],
        len(spawned),
        spawned[0][2].terminated,
        spawned[1][0],
    ) == (
        0,
        True,
        2,
        True,
        [
            "hermes",
            "chat",
            "--tui",
            "--continue",
            "sac:ui",
            "--create-if-missing",
        ],
    )


def test_live_owned_session_is_left_untouched(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []

    def spawn(command, *, env):
        del command, env
        process = _Process()
        spawned.append(process)
        return process

    def active_list(_state_dir):
        spawned[0].returncode = 0
        return [{"id": "live-1", "title": "sac:ui", "session_key": "stored-1"}]

    # Act
    _process, result = owner._supervise_tui(
        ["hermes", "chat", "--tui", "--continue", "sac:ui"],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=active_list,
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    assert (result, len(spawned), spawned[0].terminated) == (0, 1, False)


def test_owner_observes_the_live_session_on_every_supervision_poll(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []
    observed = []

    def spawn(command, *, env):
        del command, env
        process = _Process()
        spawned.append(process)
        return process

    def instrument(session):
        observed.append(session["id"])
        if len(observed) == 2:
            spawned[0].returncode = 0

    # Act
    _process, result = owner._supervise_tui(
        ["hermes", "chat", "--tui", "--continue", "sac:ui"],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=lambda _state_dir: [
            {"id": "live-1", "title": "sac:ui", "session_key": "stored-1"}
        ],
        on_session_observed=instrument,
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    assert (result, observed) == (0, ["live-1", "live-1"])


def test_owner_clears_periodic_turn_before_declaring_session_attached(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []
    clear_attempts = []

    def spawn(command, *, env):
        del command, env
        process = _Process()
        spawned.append(process)
        return process

    def clear(session):
        clear_attempts.append(session["id"])
        if len(clear_attempts) == 1:
            raise RuntimeError("gateway control temporarily unavailable")
        spawned[0].returncode = 0

    # Act
    _process, result = owner._supervise_tui(
        ["hermes", "chat", "--tui", "--continue", "sac:ui"],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=lambda _state_dir: [
            {"id": "live-1", "title": "sac:ui", "session_key": "stored-1"}
        ],
        on_session_attached=clear,
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    supervision = json.loads(
        (tmp_path / owner.SUPERVISION_FILE).read_text(encoding="utf-8")
    )
    assert (
        result,
        len(spawned),
        spawned[0].terminated,
        clear_attempts,
        supervision["state"],
        supervision["periodic_turns"],
    ) == (0, 1, False, ["live-1", "live-1"], "attached", "disabled")


def test_ambiguous_live_sessions_are_refused_without_restarting_tui(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []

    def spawn(command, *, env):
        del command, env
        process = _Process()
        spawned.append(process)
        return process

    # Act
    _process, result = owner._supervise_tui(
        ["hermes", "chat", "--tui", "--continue", "sac:ui"],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=lambda _state_dir: [
            {"id": "live-1", "title": "sac:ui"},
            {"id": "live-2", "title": "sac:ui"},
        ],
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    supervision = (tmp_path / owner.SUPERVISION_FILE).read_text(encoding="utf-8")
    assert (result, len(spawned), spawned[0].terminated, "identity mismatch" in supervision) == (
        70,
        1,
        False,
        True,
    )


def test_never_observed_reconcile_retry_is_idempotent(tmp_path):
    # Arrange
    gateway = _Process()
    spawned = []

    def spawn(command, *, env):
        process = _Process()
        spawned.append((list(command), env, process))
        return process

    calls = 0

    def active_list(_state_dir):
        nonlocal calls
        calls += 1
        if calls == 5:
            spawned[-1][2].returncode = 0
        return []

    # Act
    _process, result = owner._supervise_tui(
        [
            "hermes",
            "chat",
            "--tui",
            "--continue",
            "sac:ui",
            "--create-if-missing",
            "--query",
            "initial task",
        ],
        env={},
        state_dir=tmp_path,
        gateway=gateway,
        spawn=spawn,
        active_list=active_list,
        sleep=lambda _seconds: None,
        monotonic=lambda: 100.0,
        poll_s=0,
        startup_grace_s=0,
    )

    # Assert
    recovery_commands = [row[0] for row in spawned[1:]]
    assert (
        result,
        len(recovery_commands),
        len({tuple(command) for command in recovery_commands}),
        any("--query" in command for command in recovery_commands),
    ) == (0, 2, 1, False)
