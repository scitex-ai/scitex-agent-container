"""Validate the declared private Python in the actual launch namespace."""

from __future__ import annotations

import os
import signal
import subprocess

from ._entry_point_gate import probe_argv_from_launch

PRIVATE_VENV = "/uvwork/venv-agent"
_READY = b"SAC_PRIVATE_PYTHON_READY\n"
_COMMAND = (
    "test -r /uvwork/venv-agent/bin/activate && "
    "test -x /uvwork/venv-agent/bin/python && "
    "exec /uvwork/venv-agent/bin/python -I -S -c "
    "'print(\"SAC_PRIVATE_PYTHON_READY\")'"
)


class PrivatePythonGateError(RuntimeError):
    """The requested private interpreter could not be qualified."""


def private_python_probe_argv(launch_argv: list[str]) -> list[str]:
    """Keep the actual image, overlay and binds; replace only the inner job."""
    argv = probe_argv_from_launch(launch_argv, script="/bin/sh")
    if not argv:
        raise PrivatePythonGateError("private Python probe has no launch image")
    return [*argv[:-1], "-c", _COMMAND]


def _run_probe(argv: list[str], *, env=None) -> tuple[int, bytes]:
    """Bound and reap this probe's own process group without a retry."""
    child = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    try:
        output, _ = child.communicate(timeout=7)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()
        raise PrivatePythonGateError("private Python probe timed out") from None
    return child.returncode, output


def assert_private_python_runs(
    config, launch_argv: list[str], *, runner=None, env=None
) -> None:
    """Refuse a requested private venv when its target cannot be qualified.

    The caller's /uvwork belongs to the caller. It is never evidence about
    this agent's interpreter. No missing environment is created or replaced.
    """
    if getattr(config, "python_venv", "") != PRIVATE_VENV:
        return
    try:
        argv = private_python_probe_argv(launch_argv)
        code, output = runner(argv) if runner else _run_probe(argv, env=env)
    except OSError as error:
        raise PrivatePythonGateError("private Python probe could not run") from error
    if code != 0 or output != _READY:
        raise PrivatePythonGateError(
            "Requested /uvwork/venv-agent is not ready in the actual launch "
            "image/overlay/bind namespace. Preserve and qualify its existing "
            "private environment; no host venv or alternate interpreter is selected."
        )
