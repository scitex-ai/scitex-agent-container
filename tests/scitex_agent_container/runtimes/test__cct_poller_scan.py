"""The poller SCAN must find pollers and must not find its own search.

The named trap this pins: ``pgrep -f telegram-server`` matches the searching
shell itself. A detector that counts its own search as a poller manufactures
the exact duplicate it exists to find — and would then report VIOLATION on a
perfectly healthy host, which is how a detector gets ignored.

No mocks. ``is_poller_argv`` is pure over an argv list, and the ``/proc``
readers are driven against a real directory tree written by the test.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

import os

from scitex_agent_container.runtimes._cct_poller_scan import (
    is_poller_argv,
    poller_from_pid,
    read_process_env,
    scan_live_pollers,
)

_BUN_POLLER = [
    "/home/ywatanabe/.bun/bin/bun",
    "run",
    "/home/y/proj/cct/ts/telegram-server.ts",
]
_SH_WRAPPED = ["/bin/sh", "-c", "exec bun run /tg/telegram-server.ts"]
_UNRELATED = ["/opt/venv-sac/bin/python", "/opt/venv-sac/bin/sac", "mcp", "start"]


def _write_proc(root, pid: int, argv: list[str], env: dict[str, str] | None = None):
    """Write a minimal ``/proc/<pid>`` with cmdline (+ environ when given)."""
    pid_dir = root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
    if env is not None:
        blob = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"
        (pid_dir / "environ").write_bytes(blob.encode())
    return pid_dir


def test_a_bun_run_poller_is_recognised():
    # Arrange
    argv = _BUN_POLLER
    # Act
    verdict = is_poller_argv(argv)
    # Assert
    assert verdict is True


def test_a_shell_wrapped_poller_is_recognised():
    # Arrange — the shape the SDK channel config actually emits.
    argv = _SH_WRAPPED
    # Act
    verdict = is_poller_argv(argv)
    # Assert
    assert verdict is True


def test_a_pgrep_searching_for_the_poller_is_not_one():
    # Arrange — THE LOAD-BEARING CASE. This argv carries the pattern and runs
    # nothing; counting it would invent a duplicate on a healthy host.
    argv = ["pgrep", "-f", "telegram-server.ts"]
    # Act
    verdict = is_poller_argv(argv)
    # Assert
    assert verdict is False


def test_a_ripgrep_over_the_source_is_not_a_poller():
    # Arrange
    argv = ["/usr/bin/rg", "-n", "telegram-server.ts", "/home/y/proj/cct"]
    # Act
    verdict = is_poller_argv(argv)
    # Assert
    assert verdict is False


def test_an_unrelated_mcp_child_is_not_a_poller():
    # Arrange
    argv = _UNRELATED
    # Act
    verdict = is_poller_argv(argv)
    # Assert
    assert verdict is False


def test_an_empty_argv_is_not_a_poller():
    # Arrange — a kernel thread has an empty cmdline.
    argv: list[str] = []
    # Act
    verdict = is_poller_argv(argv)
    # Assert
    assert verdict is False


def test_an_unreadable_environ_reads_as_none_not_empty(tmp_path):
    # Arrange — the cross-uid case: cmdline present, environ absent. ``{}``
    # here would silently claim the process carries no token.
    pid_dir = _write_proc(tmp_path, 4242, _BUN_POLLER)
    # Act
    env = read_process_env(pid_dir)
    # Assert
    assert env is None


def test_an_unreadable_environ_yields_no_fingerprint(tmp_path):
    # Arrange
    pid_dir = _write_proc(tmp_path, 4242, _BUN_POLLER)
    # Act
    poller = poller_from_pid(4242, pid_dir)
    # Assert
    assert poller.token_fp is None


def test_a_readable_token_yields_an_opaque_fingerprint(tmp_path):
    # Arrange
    pid_dir = _write_proc(
        tmp_path, 4242, _BUN_POLLER, {"CCT_BOT_TOKEN": "123456:SECRET-VALUE"}
    )
    # Act
    poller = poller_from_pid(4242, pid_dir)
    # Assert
    assert poller.token_fp.startswith("sha256:") and len(poller.token_fp) == 19


def test_the_fingerprint_contains_no_part_of_the_token(tmp_path):
    # Arrange
    pid_dir = _write_proc(
        tmp_path, 4242, _BUN_POLLER, {"CCT_BOT_TOKEN": "123456:SECRET-VALUE"}
    )
    # Act
    poller = poller_from_pid(4242, pid_dir)
    # Assert
    assert "SECRET" not in repr(poller) and "123456" not in repr(poller)


def test_the_owning_agent_comes_from_the_process_env(tmp_path):
    # Arrange
    pid_dir = _write_proc(
        tmp_path,
        4242,
        _BUN_POLLER,
        {"CCT_BOT_TOKEN": "t", "CCT_AGENT_ID": "scitex-agent-container"},
    )
    # Act
    poller = poller_from_pid(4242, pid_dir)
    # Assert
    assert poller.agent == "scitex-agent-container"


def test_the_scan_excludes_the_calling_process(tmp_path):
    # Arrange — a poller-shaped argv written under OUR OWN pid. A detector
    # that can appear in its own population is not measuring the host.
    _write_proc(tmp_path, os.getpid(), _BUN_POLLER, {"CCT_BOT_TOKEN": "t"})
    # Act
    found = scan_live_pollers(proc_root=tmp_path)
    # Assert
    assert found == ()


def test_the_scan_skips_non_numeric_proc_entries(tmp_path):
    # Arrange — /proc holds self, sys, meminfo … alongside the pid dirs.
    (tmp_path / "sys").mkdir()
    _write_proc(tmp_path, 4242, _BUN_POLLER, {"CCT_BOT_TOKEN": "t"})
    # Act
    found = scan_live_pollers(proc_root=tmp_path, self_pid=1)
    # Assert
    assert [p.pid for p in found] == [4242]
