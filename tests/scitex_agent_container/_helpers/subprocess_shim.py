"""Reusable test helper: fake binaries on PATH for real-subprocess tests.

Honest replacement for ``monkeypatch.setattr("subprocess.run", ...)``.
Tests that need to verify argv shape or control stdout / stderr / exit
of a shell-out can install a fake binary on PATH and let production
code invoke the real ``subprocess.run`` — which finds the shim via the
real PATH lookup, then records its argv (base64-encoded) to a log file
the test reads back.

Usage::

    def test_my_cli_calls_ssh(subprocess_shim, ...):
        ssh = subprocess_shim.install("ssh", stdout='{"ok": true}', exit=0)
        # ... invoke CLI that runs subprocess.run(["ssh", ...]) ...
        argv = subprocess_shim.argv_for("ssh")
        assert argv[-3:] == ["--", "echo", "hi"]
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Callable

import pytest


def _sh_squote(s: str) -> str:
    """POSIX single-quote ``s`` for safe literal embedding in an sh script.

    Single-quoting (not double) so a path containing ``$`` / backtick can
    never be expanded by the shell; an embedded ``'`` is closed-escaped-
    reopened the standard ``'\\''`` way.
    """
    return "'" + s.replace("'", "'\\''") + "'"


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
        """Write a lightweight ``/bin/sh`` fake binary at ``$bin_dir/<name>``.

        Each invocation appends its argv (one base64 record per call) to a
        log file so ``argv_for`` / ``invocations`` read it back unchanged.
        Returns the path of the installed shim.

        Why ``/bin/sh`` and not a Python script: the fake binary must be as
        cheap to *start* as the real ``tmux`` / ``pgrep`` / ``ssh`` / ``echo``
        C binary it doubles for. A Python-script shim pays a full interpreter
        cold start on every call, and — because the test env sets
        ``COVERAGE_PROCESS_START`` (subprocess-coverage wiring in
        ``tests/conftest.py``) — the coverage ``.pth`` shim additionally
        imports ``coverage`` at that interpreter's startup. Under a cold
        container filesystem on a loaded CI runner that startup intermittently
        blew past the ``timeout=3`` guard in production probes
        (``snapshot/_io._run`` / ``_probe_tmux``), which then caught
        ``TimeoutExpired`` (a ``SubprocessError``) and returned the missing-
        binary fallback (``''`` / ``None``) — a spurious SIF-only failure of
        ``test_run_returns_stdout`` / ``test_probe_tmux_lists_sessions_count``.
        A ``/bin/sh`` shim starts in ~1 ms like the real binary, removing the
        artifact without weakening the assertion or mocking anything.

        Encoding contract: stdout / stderr are streamed byte-exact from
        sidecar files (written here from the exact Python strings) so no shell
        escaping can mangle embedded newlines. The argv is logged as ONE line
        per call = base64 of the NUL-joined ``"$@"`` (empty line = zero args).
        base64 survives ANY argv bytes — embedded newlines, quotes,
        backslashes, non-UTF-8 — which a hand-rolled JSON/shell escaper does
        not (e.g. a multi-line ``ssh`` remote snippet). ``base64`` + ``tr`` are
        POSIX-portable (coreutils on Linux, BSD on macOS).
        """
        log = self._bin / f"{name}.argv.log"
        self._logs[name] = log
        out_file = self._bin / f"{name}.out"
        err_file = self._bin / f"{name}.err"
        out_file.write_text(stdout)
        err_file.write_text(stderr)
        script = self._bin / name
        body = (
            "#!/bin/sh\n"
            f"__log={_sh_squote(str(log))}\n"
            # One record per call: base64(NUL-joined argv) then newline. The
            # `$# > 0` guard keeps a zero-arg call an EMPTY line (a lone
            # `printf '%s\\0'` with no args would emit a spurious empty field).
            '{ if [ "$#" -gt 0 ]; then printf \'%s\\0\' "$@" | base64 | tr -d \'\\n\'; fi; '
            "printf '\\n'; } >> \"$__log\"\n"
            f"cat {_sh_squote(str(out_file))}\n"
            f"cat {_sh_squote(str(err_file))} >&2\n"
            f"exit {int(exit)}\n"
        )
        script.write_text(body)
        script.chmod(0o755)
        return script

    def argv_for(self, name: str) -> list[str] | None:
        """Return the argv (list of str) of the last invocation, or None."""
        invocations = self.invocations(name)
        return invocations[-1] if invocations else None

    def invocations(self, name: str) -> list[list[str]]:
        """Return all invocations as list of argv lists.

        Format-agnostic reader — a log line is decoded by shape so BOTH shim
        styles that share this reader keep working:

        * ``install``'s ``/bin/sh`` shim writes one base64 record per call
          (empty line = zero args); base64 never starts with ``[``.
        * hand-rolled Python shims elsewhere (e.g. ``apptainer_overlay_shim``
          in ``test__apptainer_runtime.py``, which points
          ``subprocess_shim._logs[...]`` at its own log) write a JSON array
          per call, always starting with ``[``.

        Decoding is the exact inverse of whichever writer produced the line,
        and the base64 path survives any argv bytes (embedded newlines, etc.).
        """
        log = self._logs.get(name)
        if log is None or not log.exists():
            return []
        calls: list[list[str]] = []
        for line in log.read_text().splitlines():
            if line == "":
                calls.append([])  # zero-arg call (base64 shim)
                continue
            if line[0] == "[":
                calls.append(json.loads(line))  # JSON-array shim
                continue
            raw = base64.b64decode(line)
            parts = raw.split(b"\x00")
            if parts and parts[-1] == b"":
                # Drop the trailing empty field left by the NUL terminator.
                parts = parts[:-1]
            calls.append([p.decode("utf-8", "surrogateescape") for p in parts])
        return calls

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
