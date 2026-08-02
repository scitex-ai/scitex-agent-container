"""The inbound-rail detector must FIRE, and must not fire on an unknown.

Pins the 2026-08-03 finding: five of ten live agents declared the Telegram
channel and could not receive it. Every existing signal was green — tmux alive,
a2a adapter running, heartbeat fresh, processes up 3h23m — because none of them
observed the thing that was missing.

The detector's whole value is that it can say DETACHED about an agent that
looks healthy. So the load-bearing test is the negative one; "returns attached
for a healthy agent" would pass for a function that returns the string
"attached" unconditionally.

/proc is faked as a directory tree rather than mocked — no monkeypatching, and
it exercises the real reader.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._helpers._agent_list_inbound_rail import (
    ATTACHED,
    DETACHED,
    inbound_rail_state,
    rail_state_from_cmdlines,
)

_REAL_SERVER = "/home/ywatanabe/.bun/bin/bun run /home/y/proj/cct/ts/telegram-server.ts"
_OTHER_CHILD = "/opt/venv-sac/bin/python /opt/venv-sac/bin/sac mcp start"


def _write_proc(root, pid: int, children: dict[int, str]) -> None:
    """Build a minimal /proc: <pid>/task/<pid>/children plus each child cmdline."""
    task = root / str(pid) / "task" / str(pid)
    task.mkdir(parents=True)
    (task / "children").write_text(" ".join(str(c) for c in children))
    for cpid, cmd in children.items():
        cdir = root / str(cpid)
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "cmdline").write_bytes(cmd.replace(" ", "\x00").encode())


def test_a_telegram_server_child_reads_as_attached():
    # Arrange
    cmdlines = [_OTHER_CHILD, _REAL_SERVER]
    # Act
    state = rail_state_from_cmdlines(cmdlines)
    # Assert
    assert state == ATTACHED


def test_an_agent_with_other_mcp_children_but_no_telegram_is_detached():
    # Arrange — THE LOAD-BEARING CASE. This is scitex-hub at 21:00 today: sac
    # and cards MCP servers both running, everything green, telegram absent.
    cmdlines = [_OTHER_CHILD, "/opt/venv-sac/bin/python ... scitex-cards mcp start"]
    # Act
    state = rail_state_from_cmdlines(cmdlines)
    # Assert
    assert state == DETACHED


def test_the_detector_fires_on_a_healthy_looking_agent(tmp_path):
    # Arrange — a real /proc read, agent process present, MCP children present,
    # none of them the telegram server.
    _write_proc(tmp_path, 4242, {5001: _OTHER_CHILD})
    # Act
    state = inbound_rail_state(4242, proc_root=tmp_path)
    # Assert
    assert state == DETACHED


def test_a_working_agent_reads_as_attached_through_real_proc(tmp_path):
    # Arrange
    _write_proc(tmp_path, 4242, {5001: _OTHER_CHILD, 5002: _REAL_SERVER})
    # Act
    state = inbound_rail_state(4242, proc_root=tmp_path)
    # Assert
    assert state == ATTACHED


def test_a_missing_pid_is_unknown_not_detached(tmp_path):
    # Arrange — nothing written; the agent's process is not in this /proc.
    # Reporting DETACHED here would manufacture a false alarm for every agent
    # on another host, which is how a detector gets ignored.
    # Act
    state = inbound_rail_state(4242, proc_root=tmp_path)
    # Assert
    assert state is None


def test_no_pid_recorded_is_unknown(tmp_path):
    # Arrange
    pid = None
    # Act
    state = inbound_rail_state(pid, proc_root=tmp_path)
    # Assert
    assert state is None
