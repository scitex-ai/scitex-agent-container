"""Start the Hermes gateway (and pick the TUI spawner) for the TUI owner.

Without ``--hide`` this is the plain launch: ``hermes serve`` with the API
key in its environment and the TUI started with :class:`subprocess.Popen`.

With ``--hide`` (sealed runs) the owner keeps ``/state/<name>`` and the
launching SAC installation, and starts the gateway and every TUI through
:mod:`.._sandbox._hide`, so they and every tool child they spawn see
neither. The gateway must still hold the API key, but a value in its
exec-time environment would stay readable through ``/proc/<pid>/environ`` by
the tool children it starts. The key therefore reaches it over an inherited
pipe: a short bootstrap on Hermes' own interpreter makes the process
non-dumpable, reads the key into ``os.environ`` (Hermes reads it from there
and already strips it from its children's environment), and runs the
``hermes`` console script in-process. The gateway's ready file moves out of
the hidden state dir into a private directory under the run's ``/tmp``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Mapping

__all__ = [
    "GATEWAY_ARGV",
    "SECRET_FD_ENV",
    "TOKEN_ENV",
    "gateway_bootstrap_argv",
    "gateway_ready_dir",
    "hidden_spawner",
    "spawn_hidden_gateway",
    "start_gateway",
]

GATEWAY_ARGV = ("hermes", "serve", "--host", "127.0.0.1", "--port", "0", "--isolated")
#: The Hermes gateway's API key variable.
TOKEN_ENV = "HERMES_DASHBOARD_SESSION_TOKEN"
#: Env var naming the inherited pipe fd that carries ``NAME=VALUE``.
SECRET_FD_ENV = "SAC_HIDDEN_SECRET_FD"
#: Far above any API key, far below a pipe's buffer (the write never blocks).
MAX_SECRET_BYTES = 4_096

_BOOTSTRAP = """\
import ctypes, os, runpy, sys
if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:
    raise SystemExit("[sac-hide] cannot make the gateway non-dumpable")
fd = int(os.environ.pop("SAC_HIDDEN_SECRET_FD"))
chunks = []
while chunk := os.read(fd, 65_536):
    chunks.append(chunk)
os.close(fd)
name, sep, value = b"".join(chunks).decode().partition("=")
if not sep or not name:
    raise SystemExit("[sac-hide] malformed gateway secret")
os.environ[name] = value
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path[0] = os.path.dirname(script)
runpy.run_path(script, run_name="__main__")
"""


def _script_interpreter(script: Path) -> str:
    """The absolute interpreter in ``script``'s ``#!`` line (a Python script)."""
    with open(script, "rb") as fh:
        first = fh.readline(4_096).decode("utf-8", "replace").strip()
    parts = first[2:].split() if first.startswith("#!") else []
    if not parts or not os.path.isabs(parts[0]) or "python" not in Path(parts[0]).name:
        raise RuntimeError(
            f"{script} is not a Python console script; refusing to start the "
            "Hermes gateway with its API key in a readable environment"
        )
    return parts[0]


def gateway_bootstrap_argv(argv: list[str], *, path: str | None = None) -> list[str]:
    """``argv`` (``hermes serve ...``) run in-process behind the bootstrap."""
    located = shutil.which(argv[0], path=path)
    if not located:
        raise RuntimeError(f"{argv[0]} is not on PATH; cannot start the Hermes gateway")
    script = Path(located)
    return [_script_interpreter(script), "-c", _BOOTSTRAP, str(script), *argv[1:]]


def gateway_ready_dir(state_dir: Path, *, hidden: list[str]) -> Path:
    """Where the gateway writes its ready file: a dir it can see."""
    if not hidden:
        return state_dir
    return Path(tempfile.mkdtemp(prefix="sac-hermes-"))


def hidden_spawner(hidden: list[str]) -> Callable[..., subprocess.Popen]:
    """A ``spawn(command, env=...)`` that starts ``command`` under the mask."""
    from .._sandbox._hide import hide_argv

    def spawn(command: list[str], *, env: Mapping[str, str]) -> subprocess.Popen:
        return subprocess.Popen(hide_argv(sys.executable, hidden, command), env=env)

    return spawn


def spawn_hidden_gateway(
    argv: list[str],
    *,
    env: Mapping[str, str],
    secret: tuple[str, str],
    hidden: list[str],
) -> subprocess.Popen:
    """Start the gateway under the mask, handing it ``secret`` over a pipe."""
    from .._sandbox._hide import hide_argv

    name, value = secret
    payload = f"{name}={value}".encode()
    if len(payload) > MAX_SECRET_BYTES:
        raise RuntimeError("gateway secret exceeds the bootstrap pipe limit")
    child_env = {k: v for k, v in env.items() if k != name}
    command = gateway_bootstrap_argv(argv, path=child_env.get("PATH"))
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = -1
        child_env[SECRET_FD_ENV] = str(read_fd)
        return subprocess.Popen(
            hide_argv(sys.executable, hidden, command),
            env=child_env,
            pass_fds=(read_fd,),
        )
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)


def start_gateway(
    *, env: dict[str, str], token: str, hidden: list[str]
) -> tuple[subprocess.Popen, Callable[..., subprocess.Popen]]:
    """Start ``hermes serve``; return it and the spawner for the TUI."""
    if not hidden:
        env[TOKEN_ENV] = token
        return subprocess.Popen(list(GATEWAY_ARGV), env=env), subprocess.Popen
    gateway = spawn_hidden_gateway(
        list(GATEWAY_ARGV), env=env, secret=(TOKEN_ENV, token), hidden=hidden
    )
    return gateway, hidden_spawner(hidden)
