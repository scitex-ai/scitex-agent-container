from __future__ import annotations

from scitex_agent_container.runtimes import _hermes_tui_owner as owner


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
