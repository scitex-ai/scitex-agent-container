"""Ship one probe script to the target, over the only route a container has.

THE ROUTE, AND WHY IT IS NOT PLAIN SSH. An agent runs inside an apptainer SIF
whose ``$HOME/.ssh`` is a READ-ONLY bind. OpenSSH there cannot create its
ControlMaster socket and refuses the connection outright::

    cannot bind to path /home/agent/.ssh/.control... : Read-only file system

So the container does not ssh. It asks the ``sac listen`` daemon on the BARE
HOST to run the ssh for it (:mod:`._host_exec_client`, the same ``/v1/host_exec``
bypass the ``host_exec_local`` MCP tool uses), and the host's ssh — with a
writable home, the operator's ``~/.ssh/config``, and the fleet's keys — reaches
the target. Measured 2026-08-09 against scitex-compute-03/-04, scitex-nas-01/-02
/-03 and mba.

WHY THE SCRIPT IS ONE ARGV ELEMENT. ssh joins everything after the destination
with spaces and hands the result to the REMOTE login shell, which re-parses it.
Passing ``["sh", "-c", script]`` therefore does NOT do what it reads like: the
remote shell sees ``sh -c <first-word-of-script> <second-word> …`` and runs the
first word alone with the rest as positional parameters. Measured the same day:
``ssh host sh -c 'echo MARK; uname -s'`` printed an EMPTY line where ``MARK``
should have been — ``echo`` ran with ``MARK`` as ``$0`` — and then ``uname``
executed anyway, from the OUTER shell. Half the script silently ran in a
different shell than intended. One string, one parse, no surprises.

WHY NOT :func:`.._state.host_config.build_ssh_argv`. It is sac's remote-dispatch
choke point and this deliberately does not ride it, for two reasons that are
specific to shipping a SCRIPT from a CONTAINER:

    * Its ``env_preamble`` branch renders ``bash -c '<preamble> && <shlex.join
      (command)>'``. That is right for an argv command list and wrong for a
      script: ``shlex.join`` collapses the whole script into ONE quoted word, so
      bash would try to execute a command whose name is the entire program.
      Here the preamble is prepended INSIDE the script instead, which also works
      on the busybox targets where ``bash`` does not exist.
    * Its ControlMaster options are resolved in the CONTAINER (a ``$TMPDIR``
      path) and would then be used by ssh on the HOST — a path computed on one
      machine and bound on another.

The ``via:`` ProxyJump chain and ``env_preamble`` still come from the same
``config.yaml``, so multi-hop peers keep working; only the argv rendering is
local to this file.

AN SSH FAILURE IS EVIDENCE; A TRANSPORT FAILURE IS NOT. If the listen daemon
cannot be reached, we have learned NOTHING about the target and say so by
raising. If ssh itself ran and failed, the target did not answer us, and that is
a measurement the caller is entitled to use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

__all__ = [
    "ProbeTransportError",
    "RemoteRun",
    "build_probe_argv",
    "peer_preamble",
    "run_probe_script",
]

#: ssh reserves exit 255 for its OWN failures (handshake, auth, no route) as
#: opposed to relaying the remote command's status. It is the one exit code that
#: means "the connection did not happen".
SSH_FAILURE_EXIT = 255

#: Wall-clock cap for the whole batched probe on the host side. One ssh round
#: trip that also runs a handful of `[ -e ]` tests and two python one-liners;
#: anything past this is a wedged host, and a dry run must not hang on one.
DEFAULT_TIMEOUT_S = 60.0


class ProbeTransportError(RuntimeError):
    """The probe never reached the target — nothing about it was measured.

    Distinct from a target that answered badly. Raised only when the container
    could not get its command onto the host at all, which must leave EVERY fact
    unknown rather than any fact false.
    """


@dataclass(frozen=True)
class RemoteRun:
    """The raw result of one batched probe: what ssh printed, and how it exited."""

    stdout: str
    stderr: str
    exit_code: int

    @property
    def ssh_failed(self) -> bool:
        """True when ssh itself could not connect (as opposed to the script failing)."""
        return self.exit_code == SSH_FAILURE_EXIT


def peer_preamble(host: str, peers=None) -> str:
    """The peer's ``env_preamble`` from ``config.yaml``, or ``""``.

    Load-bearing on the hosts the fleet is actually moving onto: ssh runs a
    NON-login shell, sac lives in a venv on scitex-compute-03/-04, and without
    the preamble the two facts that ask the target's own validator go
    unanswered there. Missing config, an unknown peer, or a malformed file all
    yield ``""`` — a probe must still run against a host that ``~/.ssh/config``
    knows and ``config.yaml`` does not.
    """
    spec = _peer_spec(host, peers)
    return spec.joined_preamble() if spec is not None else ""


def _peer_spec(host: str, peers=None):
    if peers is None:
        try:
            from .._state.host_config import load

            peers = load().peers
        except Exception:  # stx-allow: fallback (reason: an unreadable config must degrade to a direct ssh, not abort a preflight that ~/.ssh/config can still serve)
            return None
    try:
        return peers.get(host)
    except Exception:  # stx-allow: fallback (reason: PeersMap raises MovingAliasError for retired names; a probe of an unregistered host is still a legitimate probe)
        return None


def build_probe_argv(host: str, script: str, peers=None) -> list[str]:
    """Render the ssh argv the HOST will run, with the script as one element.

    ``-o BatchMode=yes`` because there is no terminal on the far end of a
    listen-daemon exec — a password or known-hosts prompt would hang until the
    timeout instead of failing. ``StrictHostKeyChecking=accept-new`` matches
    sac's existing dispatch policy: accept a first-touch key, still refuse a
    CHANGED one.

    A host with no ``config.yaml`` entry is addressed directly by name; the
    host's own ``~/.ssh/config`` resolves it (scitex-nas-01 and -02 are reached
    exactly that way today). Refusing to probe an unregistered host would turn a
    measurable fact into an unknown for a bookkeeping reason.
    """
    if not host:
        raise ValueError("build_probe_argv: host must be non-empty")
    argv = ["ssh"]
    spec = _peer_spec(host, peers)
    target = host
    if spec is not None:
        target = spec.ssh or host
        chain = spec.jump_chain(peers) if peers is not None else _chain(spec)
        if chain:
            argv += ["-J", ",".join(chain)]
    argv += [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
        target,
        script,
    ]
    return argv


def _chain(spec) -> list[str]:
    if not spec.via:
        return []
    try:
        from .._state.host_config import load

        return spec.jump_chain(load().peers)
    except Exception:  # stx-allow: fallback (reason: an unresolvable jump chain must degrade to a direct attempt, whose failure is then an honest observation)
        return []


def run_probe_script(
    host: str,
    script: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    peers=None,
) -> RemoteRun:
    """Run ``script`` on ``host`` through the listen daemon and return what it said.

    ``exec_fn`` is the seam: it takes the same arguments as
    :func:`.._lifecycle._host_exec_client.request_host_exec` and returns its
    ``{"exit_code", "stdout", "stderr", …}`` body. Tests pass a real callable
    that returns canned output, so no transport is mocked and no monkeypatching
    is needed.

    Raises :class:`ProbeTransportError` when the exec never happened, when it
    timed out, or when the body is not the documented shape. It never returns a
    fabricated success — a caller that cannot tell "the target said no" from "I
    never asked" is the failure this whole feature exists to prevent.
    """
    if exec_fn is None:
        from ._host_exec_client import request_host_exec

        exec_fn = request_host_exec

    argv = build_probe_argv(host, script, peers)
    try:
        body = exec_fn(argv, timeout_s=timeout_s)
    except Exception as exc:  # stx-allow: fallback (reason: re-raised as ProbeTransportError so every fact stays UNKNOWN; nothing is swallowed and nothing degrades to False)
        raise ProbeTransportError(
            f"could not run the probe on the host for {host}: {type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(body, dict):
        raise ProbeTransportError(
            f"host_exec returned {type(body).__name__}, not the documented body"
        )
    if body.get("timed_out"):
        raise ProbeTransportError(
            f"the probe of {host} timed out after {timeout_s:.0f}s; "
            "nothing about the target was measured"
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
