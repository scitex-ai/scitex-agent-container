"""Reusable test helper: fake binaries on PATH for real-subprocess tests.

Honest replacement for ``monkeypatch.setattr("subprocess.run", ...)``.
Tests that need to verify argv shape or control stdout / stderr / exit
of a shell-out can install a fake binary on PATH and let production
code invoke the real ``subprocess.run`` — which finds the shim via the
real PATH lookup, then writes its argv (as JSON) to a log file the
test reads back.

Usage::

    def test_my_cli_calls_ssh(subprocess_shim, ...):
        ssh = subprocess_shim.install("ssh", stdout='{"ok": true}', exit=0)
        # ... invoke CLI that runs subprocess.run(["ssh", ...]) ...
        argv = subprocess_shim.argv_for("ssh")
        assert argv[-3:] == ["--", "echo", "hi"]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable

import pytest


class _ShimController:
    def __init__(self, bin_dir: Path) -> None:
        self._bin = bin_dir
        self._logs: dict[str, Path] = {}

    def install(
        self,
        name: str,
        *,
        exit: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> Path:
        """Write a Python-script fake binary at ``$bin_dir/<name>``.

        Each invocation appends its argv (JSON-encoded list) to a log
        file. Returns the path of the installed shim.
        """
        log = self._bin / f"{name}.argv.jsonl"
        self._logs[name] = log
        script = self._bin / name
        body = (
            f"#!{sys.executable}\n"
            "import json, sys\n"
            f"with open({json.dumps(str(log))}, 'a') as fh:\n"
            "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"sys.stdout.write({json.dumps(stdout)})\n"
            f"sys.stderr.write({json.dumps(stderr)})\n"
            f"sys.exit({int(exit)})\n"
        )
        script.write_text(body)
        script.chmod(0o755)
        return script

    def argv_for(self, name: str) -> list[str] | None:
        """Return the argv (list of str) of the last invocation, or None."""
        invocations = self.invocations(name)
        return invocations[-1] if invocations else None

    def invocations(self, name: str) -> list[list[str]]:
        """Return all invocations as list of argv lists."""
        log = self._logs.get(name)
        if log is None or not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]

    def call_count(self, name: str) -> int:
        return len(self.invocations(name))


@pytest.fixture
def subprocess_shim(tmp_path: Path):
    """PATH-prepended fake-binary controller. Auto-restores PATH.

    Yields a ``_ShimController`` with ``install(name, ...)``,
    ``argv_for(name)``, ``invocations(name)``, ``call_count(name)``.
    """
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir()
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    try:
        yield _ShimController(bin_dir)
    finally:
        os.environ["PATH"] = saved_path


@pytest.fixture
def env_save_restore():
    """Generic env save/restore — track keys you mutate, auto-revert."""
    saved: dict[str, str | None] = {}

    def _set(key: str, value: str) -> None:
        if key not in saved:
            saved[key] = os.environ.get(key)
        os.environ[key] = value

    def _delete(key: str) -> None:
        if key not in saved:
            saved[key] = os.environ.get(key)
        os.environ.pop(key, None)

    class _Env:
        set: Callable[[str, str], None] = staticmethod(_set)
        delete: Callable[[str], None] = staticmethod(_delete)

    try:
        yield _Env
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
