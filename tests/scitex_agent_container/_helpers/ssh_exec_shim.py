"""Reusable test helper: an ``ssh`` that runs the remote command LOCALLY.

Honest replacement for mocking ``subprocess.run`` (STX-NM002). Sibling of
:mod:`ssh_http_shim` (which stands in for ssh by performing a real HTTP
POST); this one stands in for ssh by performing a real LOCAL EXEC.

Production code builds its REAL ssh argv (via
``_state.host_config.build_ssh_argv``, so the peer's ``ssh:`` target,
``via:`` ProxyJump chain and BatchMode/ControlMaster options are all real),
the REAL ``subprocess.run`` resolves ``ssh`` through the REAL ``$PATH``,
and this shim then executes the REAL remote command — ``mkdir``, ``dd``,
``chmod``, ``stat``, ``mv`` — against a REAL directory tree in ``tmp_path``,
with the REAL bytes flowing over the REAL stdin pipe.

Faithful to OpenSSH semantics: everything after the ``--`` separator is
joined with spaces and handed to a shell, because that is exactly what
sshd does with the post-host argv. A quoting bug introduced later
therefore breaks the test the same way it would break production.

``install_binary`` additionally materialises any other real executable on
the same PATH dir, so a test can model a hostile peer (a ``chmod`` that
lies, a ``dd`` whose stream died) with a real program rather than a mock.

Usage::

    def test_push(ssh_exec_shim, tmp_path):
        push_snapshot("work", local, transport=resolve_peer_transport("spartan"))
        assert oct(remote.stat().st_mode)[-3:] == "600"
        assert "spartan-login1" in " ".join(ssh_exec_shim.invocations()[0])
"""

from __future__ import annotations

import base64
import os
import shlex
from pathlib import Path

import pytest

# Everything after `--` is joined with spaces and re-parsed by a shell —
# precisely what OpenSSH + sshd do with the post-host argv. stdin, stdout,
# stderr and the exit status all pass through untouched.
_SSH_SOURCE = r"""#!/bin/sh
printf '%s\0' "$@" | base64 | tr -d '\n' >> __LOG__
printf '\n' >> __LOG__
__seen=0
__cmd=
for __a in "$@"; do
  if [ "$__seen" = 1 ]; then
    __cmd="$__cmd $__a"
  elif [ "$__a" = "--" ]; then
    __seen=1
  fi
done
if [ "$__seen" != 1 ]; then
  echo "shim ssh: no -- separator in argv" >&2
  exit 2
fi
exec sh -c "$__cmd"
"""


class _SshExecShim:
    """Controller for the local-exec ssh shim."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin = bin_dir
        self.log = bin_dir / "ssh.argv.log"

    def install(self) -> Path:
        """Materialise the local-exec ``ssh`` at ``$bin_dir/ssh``."""
        return self.install_binary("ssh", _SSH_SOURCE)

    def install_binary(self, name: str, source: str) -> Path:
        """Materialise any real ``/bin/sh`` executable on the shim PATH.

        ``__LOG__`` in ``source`` is substituted with the shell-quoted argv
        log path, so a shim can record its own invocations if it wants to.
        """
        script = self.bin / name
        script.write_text(source.replace("__LOG__", shlex.quote(str(self.log))))
        script.chmod(0o755)
        return script

    def invocations(self) -> list[list[str]]:
        """Every ssh argv, decoded. One record per call (base64 of NUL-joined)."""
        if not self.log.exists():
            return []
        calls: list[list[str]] = []
        for line in self.log.read_text().splitlines():
            if not line:
                calls.append([])
                continue
            parts = base64.b64decode(line).split(b"\x00")
            if parts and parts[-1] == b"":
                parts = parts[:-1]
            calls.append([p.decode("utf-8", "surrogateescape") for p in parts])
        return calls


@pytest.fixture
def ssh_exec_shim(tmp_path: Path):
    """PATH-prepended ``ssh`` that executes the remote command locally.

    Yields an installed :class:`_SshExecShim`. Auto-restores ``PATH``.
    """
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    controller = _SshExecShim(bin_dir)
    controller.install()
    try:
        yield controller
    finally:
        os.environ["PATH"] = saved_path
