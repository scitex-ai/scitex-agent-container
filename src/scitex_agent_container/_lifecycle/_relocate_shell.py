"""One way to run a script on a host — whichever host — and one way to read the answer.

Everything a relocation does on a machine goes through here, so there is exactly
one place where "how do we reach that host" can be wrong.

THE ROUTE IS THE PROBE'S ROUTE, for the reason :mod:`_relocate_probe_ssh`
documents at length: an agent runs inside a SIF whose ``$HOME/.ssh`` is a
READ-ONLY bind, so OpenSSH there cannot create its ControlMaster socket and
refuses outright. The container therefore does not ssh. It asks the ``sac
listen`` daemon on the BARE HOST to run the command, and the host's ssh — with a
writable home, the operator's ``~/.ssh/config`` and the fleet's keys — reaches
the target.

LOCAL IS NOT A SPECIAL CASE OF REMOTE. A command aimed at the coordinator's own
host runs as ``sh -c <script>`` directly, not as ``ssh <myself> …``. That is one
less connection that can fail, and it is the only way the SOURCE side works at
all when the source IS where the coordinator runs, which is the ordinary case.
It also removes the remote-shell re-parse hazard: for a local exec ``["sh",
"-c", script]`` means what it reads like, because nothing re-parses it.

MARKER LINES, NOT EXIT CODES. Every script prints its answers on prefixed lines
and the parser reads those. A shell that also emits a warning, a peer preamble
that prints a banner, an ``ls`` that partially failed — none of them can be
mistaken for a measurement, and a section that fails costs only its own answer.

AN EXEC THAT NEVER HAPPENED IS NOT A MEASUREMENT. Every path here raises
:class:`.._relocate_probe_ssh.ProbeTransportError` when the command could not be
run at all, and callers return "not measured" (``None``) when it ran and could
not answer. A caller that cannot tell those apart will eventually read "no files"
off a directory it never managed to list.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable, Sequence

from ._relocate_probe_ssh import (
    ProbeTransportError,
    RemoteRun,
    peer_preamble,
    run_probe_script,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MARK_PATH",
    "Shell",
    "marked",
    "one_marked",
    "quote",
    "resolved_path",
    "run_argv_on_host",
    "shell_for",
]

#: A directory listing, a handful of ``wc`` calls, a ``mv``. Generous enough for
#: a loaded host, short enough that a wedged one does not hang a relocation.
DEFAULT_TIMEOUT_S = 120.0

MARK_PATH = "TX-PATH="


def quote(value: str) -> str:
    """Quote one path/name for a POSIX shell. Never interpolate a bare path."""
    return shlex.quote(value)


@dataclass(frozen=True)
class Shell:
    """One host to run scripts on, and whether reaching it needs ssh at all.

    ``preamble`` is the peer's ``env_preamble`` from ``config.yaml``, prepended
    INSIDE the script rather than rendered as ``bash -c``, for exactly the reason
    :mod:`_relocate_probe_ssh` gives: ssh hands ONE string to the remote login
    shell, and the fleet's targets do not all have bash. It is load-bearing on
    the hosts being moved onto — sac lives in a venv on scitex-compute-03/-04 and
    ssh runs a non-login shell, so without the preamble ``sac`` is not on PATH.
    """

    host: str
    is_local: bool = False
    preamble: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("Shell.host must be non-empty")

    def script(self, body: str) -> str:
        return f"{self.preamble}\n{body}" if self.preamble else body

    def run(
        self,
        body: str,
        *,
        exec_fn: Callable[..., dict] | None = None,
        timeout_s: float | None = None,
    ) -> RemoteRun:
        """Run ``body`` on this host and return what it said."""
        script = self.script(body)
        wait = self.timeout_s if timeout_s is None else timeout_s
        if not self.is_local:
            return run_probe_script(self.host, script, exec_fn=exec_fn, timeout_s=wait)
        return run_argv_on_host(
            ["sh", "-c", script],
            exec_fn=exec_fn,
            timeout_s=wait,
            what=f"the local host ({self.host})",
        )


def run_argv_on_host(
    argv: Sequence[str],
    *,
    exec_fn: Callable[..., dict] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    what: str = "the host",
) -> RemoteRun:
    """Run ``argv`` on the BARE HOST through the listen daemon.

    ``exec_fn`` is the seam: it takes the same arguments as
    :func:`._host_exec_client.request_host_exec` and returns its documented body.
    Tests pass a real callable returning canned output, so nothing is mocked and
    no monkeypatching is needed.
    """
    if exec_fn is None:
        from ._host_exec_client import request_host_exec

        exec_fn = request_host_exec

    try:
        body = exec_fn(list(argv), timeout_s=timeout_s)
    except Exception as exc:  # stx-allow: fallback (reason: re-raised as ProbeTransportError so the caller records NOT MEASURED rather than a false negative)
        raise ProbeTransportError(
            f"could not run the command on {what}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise ProbeTransportError(
            f"host_exec returned {type(body).__name__}, not the documented body"
        )
    if body.get("timed_out"):
        raise ProbeTransportError(
            f"the command on {what} timed out after {timeout_s:.0f}s; nothing was measured"
        )
    exit_code = body.get("exit_code")
    if not isinstance(exit_code, int):
        raise ProbeTransportError(
            f"host_exec body carried no integer exit_code (got {exit_code!r})"
        )
    return RemoteRun(
        stdout=str(body.get("stdout") or ""),
        stderr=str(body.get("stderr") or ""),
        exit_code=exit_code,
    )


def marked(run: RemoteRun, marker: str) -> list[str]:
    """Every ``<marker><value>`` line's value, in the order printed."""
    return [
        ln[len(marker) :].strip()
        for ln in run.stdout.splitlines()
        if ln.startswith(marker)
    ]


def one_marked(run: RemoteRun, marker: str) -> str | None:
    """The first value for ``marker``, or ``None`` when the line never appeared.

    ``None`` is "the script did not answer", which every caller must treat as
    UNKNOWN — distinct from an answer of ``"no"``.
    """
    values = marked(run, marker)
    return values[0] if values else None


def resolved_path(
    shell: Shell,
    path: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> str | None:
    """``readlink -f`` on ``shell``'s own filesystem, or ``None``.

    The one question :mod:`_relocate_transport_paths` refuses to answer locally,
    for the reason it states: resolving here would resolve against the SOURCE's
    filesystem and name a directory the target's runner will never read. So it is
    asked on the machine whose answer is wanted.
    """
    if not path:
        return None
    body = f"printf '{MARK_PATH}%s\\n' \"$(readlink -f {quote(path)} 2>/dev/null)\""
    run = shell.run(body, exec_fn=exec_fn)
    return one_marked(run, MARK_PATH) or None


def shell_for(
    host: str, *, local_host: str | None, timeout_s: float = DEFAULT_TIMEOUT_S
) -> Shell:
    """A :class:`Shell` for ``host``, local when it IS the coordinator's host.

    ``local_host`` is passed in rather than discovered, so "am I already standing
    on this machine" is decided ONCE by the caller from an observation, instead of
    each call site consulting a hostname that a container answers differently
    from the bare host it runs on.
    """
    is_local = bool(local_host) and host == local_host
    return Shell(
        host=host,
        is_local=is_local,
        preamble="" if is_local else peer_preamble(host),
        timeout_s=timeout_s,
    )
