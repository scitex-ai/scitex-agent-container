"""SSH ProxyJump supervisor — keeps an ``ssh -L`` forward alive.

Spawned as a child process by :class:`TunnelManager.up` to keep an
``ssh -N -L <local>:localhost:<remote> -J <jump> <target>`` tunnel
alive for the lifetime of an agent. Wrapping the ssh invocation in a
supervisor lets us:

* Respawn on disconnect — a transient bastion drop doesn't strand the
  agent on a dead forward.
* Log every spawn / exit with a timestamp + return code to stderr so
  the operator can diagnose a permanent failure via the agent's
  stdout.log without re-running anything.
* Honour SIGTERM cleanly — sac's stop path SIGTERMs the supervisor,
  which forwards the signal to the ssh child and exits 0 instead of
  respawning.

The supervisor is intentionally a separate ``__main__`` module so the
:class:`TunnelManager` can pass ``[sys.executable, "-m", ...]`` as the
default supervisor argv but tests can substitute a one-liner that
doesn't require real ssh (e.g. a fake that just opens a listening
socket and selects forever).

Fixed ssh options (always set, before any operator overrides)
-------------------------------------------------------------

* ``-N``                                 — no remote command.
* ``-o ServerAliveInterval=30``          — keepalive cadence.
* ``-o ServerAliveCountMax=3``           — disconnect after 3 misses.
* ``-o ExitOnForwardFailure=yes``        — fail fast if the bind dies.
* ``-o BatchMode=yes``                   — no interactive prompts.

Operator ``ssh_opts`` (from :class:`TunnelSpec.ssh_opts`) are appended
verbatim AFTER these so a later ``-o`` override wins (per ssh's
last-wins semantics).
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time


def _now() -> str:
    """ISO-ish wall-clock stamp for the supervisor log lines."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _log(stream, msg: str) -> None:
    """Write one supervisor log line with a leading timestamp + tag."""
    print(f"[sac:tunnel-supervisor {_now()}] {msg}", file=stream, flush=True)


def _build_ssh_argv(args: argparse.Namespace) -> list[str]:
    """Compose the ``ssh`` argv from the parsed supervisor flags."""
    argv = [
        "ssh",
        "-N",
        "-L",
        f"{args.local_port}:localhost:{args.remote_port}",
        "-J",
        args.jump,
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "BatchMode=yes",
        args.target,
    ]
    if args.ssh_opt:
        argv.extend(args.ssh_opt)
    return argv


def _parse_argv(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scitex_agent_container._network._tunnel_supervisor",
        description="Keep an ssh -L ProxyJump tunnel alive.",
    )
    p.add_argument("--jump", required=True, help="ssh -J jump host alias.")
    p.add_argument("--target", required=True, help="ssh target host (after the jump).")
    p.add_argument(
        "--remote-port", required=True, type=int, help="Port on the target host."
    )
    p.add_argument("--local-port", required=True, type=int, help="Local bind port.")
    p.add_argument(
        "--backoff",
        type=float,
        default=2.0,
        help="Sleep seconds between respawns (default 2).",
    )
    p.add_argument(
        "--ssh-opt",
        action="append",
        default=[],
        help="Extra ssh argv tokens, appended after the fixed -o options.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Supervisor entry point. Respawns ssh until SIGTERM, returns 0 on clean exit."""
    args = _parse_argv(argv)
    stderr = sys.stderr

    # State shared with the SIGTERM handler so we can forward the
    # signal to the ssh child and exit cleanly without respawning.
    state: dict = {"child": None, "shutting_down": False}

    def _on_sigterm(signum, frame):
        """SIGTERM → forward to ssh child, mark shutdown, exit on next loop."""
        state["shutting_down"] = True
        child = state.get("child")
        if child is not None and child.poll() is None:
            _log(stderr, f"SIGTERM received; forwarding to ssh pid={child.pid}")
            try:
                child.terminate()
            except (ProcessLookupError, OSError) as exc:
                _log(stderr, f"could not signal ssh child: {exc!r}")

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    _log(
        stderr,
        f"starting supervisor: jump={args.jump!r} target={args.target!r} "
        f"local_port={args.local_port} remote_port={args.remote_port} "
        f"backoff={args.backoff}",
    )

    while not state["shutting_down"]:
        ssh_argv = _build_ssh_argv(args)
        _log(stderr, f"spawning ssh argv={ssh_argv}")
        try:
            child = subprocess.Popen(ssh_argv)
        except FileNotFoundError as exc:
            # ssh binary missing — bail loudly; respawning forever
            # would just spin the CPU with the same error.
            _log(stderr, f"ssh binary not found ({exc}); aborting supervisor")
            return 127
        state["child"] = child
        rc = child.wait()
        state["child"] = None
        _log(stderr, f"ssh child exited rc={rc}")
        if state["shutting_down"]:
            _log(stderr, "shutdown flag set; exiting supervisor cleanly")
            return 0
        # Respawn after backoff so a transient bastion drop doesn't
        # silently strand the agent. Operators see one log line per
        # respawn so a permanent failure is visible.
        time.sleep(args.backoff)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
