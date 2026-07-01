"""Port-holder discovery + self-heal for the ``sac listen`` restart.

The incident this serves (card ``sac-listen-restart-selfheal-cli``,
2026-06-26): a wedged ``sac listen`` remnant was still **bound to the
port** but never answered HTTP — ``curl`` hung forever. The flock
pidfile only names the *tracked* daemon; this remnant was UNtracked
(or the operator had already ``rm``-ed the pidfile), so the recovery
needs a pidfile-independent way to find and kill whatever holds the
port. That manual surgery was ``lsof``/``pkill``/``setsid`` by hand —
this module is its deterministic codification.

Responsibilities (all pure-stdlib + external-tool, NO new dep):

1. :func:`port_is_bound` — a socket TCP-connect probe. The *trigger*:
   "is anything holding this port right now?" A half-dead uvicorn that
   accepts the connection but never replies still shows as bound here.

2. :func:`port_holder_pids` — resolve the holding PID(s) via
   ``lsof`` → ``ss`` → ``fuser`` (whichever is installed). Empty list
   when no tool finds a holder, so the caller can fail loud rather
   than guess.

3. :func:`clear_wedged_port_holders` — the self-heal: probe, resolve,
   force-kill, re-probe; loud :class:`PortHealResult.error` if the
   port stays held.

4. :func:`diagnose_unhealthy` — name the REAL cause when a relaunched
   daemon is not serving (``port still held by PID X`` vs
   ``bind failed``).

The process-termination primitive (``terminate_fn``) and ``sleep_fn``
are passed in by the caller (``_restart``) rather than imported, to
avoid a circular dependency and keep the SIGTERM→SIGKILL escalation +
its test seams owned by ``_restart``. The two discovery seams
(``_probe_bound`` / ``_resolve_pids``) live here and are swapped via a
save/restore context manager in tests (PA-306 / STX-NM001-003 — no
MagicMock).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "PortHealResult",
    "clear_wedged_port_holders",
    "diagnose_unhealthy",
    "port_holder_pids",
    "port_is_bound",
]

# Test seam — mirrors ``_restart._run_subprocess``.
_run_subprocess: Callable[..., subprocess.CompletedProcess] = subprocess.run


def port_is_bound(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """Return ``True`` iff *something* is listening on ``host:port``.

    A pure-socket TCP connect — no dependency, no external tool. A
    wedged remnant that accepts the connection but never answers HTTP
    still shows up here as "bound". ``127.0.0.1`` is substituted for a
    wildcard ``0.0.0.0`` / ``::`` bind so an all-interfaces listener is
    still detected on loopback. Refused / timeout → ``False``.
    """
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        with socket.create_connection((probe_host, port), timeout=timeout):
            return True
    except OSError:
        return False


_LSOF_PID_RE = re.compile(r"^p(\d+)$")
_SS_PID_RE = re.compile(r"pid=(\d+)")


def port_holder_pids(port: int) -> list[int]:
    """Return the PIDs of processes LISTENING on TCP ``port``.

    Tries ``lsof`` first (most precise: filters to LISTEN), then ``ss``
    and finally ``fuser``. An absent tool (``FileNotFoundError``) or a
    non-zero / error exit advances to the next. Empty list when no tool
    finds a holder. The own-PID is never included.
    """
    self_pid = os.getpid()
    for finder in (_lsof_pids, _ss_pids, _fuser_pids):
        try:
            pids = finder(port)
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
        found = sorted({p for p in pids if p > 0 and p != self_pid})
        if found:
            return found
    return []


def _lsof_pids(port: int) -> list[int]:
    """``lsof -nP -iTCP:<port> -sTCP:LISTEN -Fp`` → listening PIDs.

    ``-F p`` machine-readable output emits one ``p<pid>`` line per
    holder. ``-sTCP:LISTEN`` avoids grabbing a transient client socket
    on the same port number.
    """
    proc = _run_subprocess(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        m = _LSOF_PID_RE.match(line.strip())
        if m:
            pids.append(int(m.group(1)))
    return pids


def _ss_pids(port: int) -> list[int]:
    """``ss -ltnpH 'sport = :<port>'`` → listening PIDs.

    The ``users:(("proc",pid=1234,fd=7))`` column carries the PID; we
    extract every ``pid=<n>``.
    """
    proc = _run_subprocess(
        ["ss", "-ltnpH", f"sport = :{port}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )
    return [int(m) for m in _SS_PID_RE.findall(proc.stdout or "")]


def _fuser_pids(port: int) -> list[int]:
    """``fuser <port>/tcp`` → PIDs (last-resort, coarsest).

    Output is ``<port>/tcp:   <pid> <pid> ...`` — the PIDs follow the
    colon, and the ``<port>/tcp:`` prefix carries the port number which
    must NOT be mistaken for a PID. We take only the text AFTER the
    last colon on each line and parse bare integers there. Lines with
    no colon (rare/odd builds) are parsed whole. Prints to stdout
    (newer builds) or stderr; we read both. Coarser than ``lsof``/``ss``
    (no LISTEN vs ESTABLISHED distinction) so it is tried last.
    """
    proc = _run_subprocess(
        ["fuser", f"{port}/tcp"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )
    pids: list[int] = []
    for line in (f"{proc.stdout or ''}\n{proc.stderr or ''}").splitlines():
        after = line.rsplit(":", 1)[-1] if ":" in line else line
        pids.extend(int(tok) for tok in re.findall(r"\d+", after))
    return pids


# Discovery seams — swapped on THIS module in tests so the self-heal
# can be driven without a real listener (and so a test never probes /
# kills the live central listen on the dev host's 7878).
_probe_bound: Callable[[str, int], bool] = port_is_bound
_resolve_pids: Callable[[int], list[int]] = port_holder_pids


@dataclass(frozen=True)
class PortHealResult:
    """Outcome of the wedged-port-holder self-heal step.

    ``killed`` is the PID(s) force-killed off the port. ``error`` is
    non-empty ONLY when the port is still bound but we could neither
    name a holder nor free it — a loud, actionable failure rather than
    a silent relaunch into ``EADDRINUSE``.
    """

    killed: tuple[int, ...] = ()
    error: str = ""


def clear_wedged_port_holders(
    *,
    host: str,
    port: int,
    grace_secs: float,
    force: bool,
    terminate_fn: Callable[..., bool],
    sleep_fn: Callable[[float], None],
    poll_interval: float,
) -> PortHealResult:
    """Free the port from an UNtracked wedged remnant ("curl hangs").

    Called AFTER the pidfile-tracked daemon is stopped. Socket-probe
    the port; if free, no-op. If bound, resolve the holding PID(s) and
    force-kill each via ``terminate_fn`` (``force`` → SIGKILL, else
    TERM→KILL), then re-probe. A still-bound port — or a bound port
    with no resolvable holder — returns a LOUD
    :attr:`PortHealResult.error` so the caller fails loud instead of
    relaunching into ``EADDRINUSE``. Pure-logic + injected seams;
    never raises.
    """
    if not _probe_bound(host, port):
        return PortHealResult()

    holders = _resolve_pids(port)
    if not holders:
        return PortHealResult(
            error=(
                f"port {port} is still held but no holding PID could be "
                f"resolved (no lsof/ss/fuser, or it lists no PID). "
                f"Refusing to relaunch into EADDRINUSE — inspect with "
                f"`sudo lsof -iTCP:{port} -sTCP:LISTEN`."
            )
        )

    killed: list[int] = []
    for pid in holders:
        terminate_fn(pid, grace_secs=grace_secs, force_kill=force)
        killed.append(pid)

    sleep_fn(poll_interval)  # let the kernel reap the socket, then re-probe
    if _probe_bound(host, port):
        survivors = _resolve_pids(port) or killed
        survivor_str = ", ".join(str(p) for p in survivors)
        return PortHealResult(
            killed=tuple(killed),
            error=(
                f"port {port} still held by PID {survivor_str} after "
                f"force-kill — the holder is unkillable from this user "
                f"(zombie / different uid / uninterruptible). Inspect "
                f"manually; may need `sudo kill -9 {survivor_str}`."
            ),
        )
    return PortHealResult(killed=tuple(killed))


def diagnose_unhealthy(
    *, host: str, port: int, deadline_secs: float, health_path: str
) -> str:
    """Name the REAL reason a freshly-relaunched daemon is not healthy.

    A restart that can't bring the daemon up must fail loud with the
    actual cause, not a generic "did not respond". Probe the port:

    * Port IS bound but health never answered → up-but-not-serving (a
      holder wedged on the socket). This is the exact state
      ``_lifecycle/_bind_watchdog.py`` (PR #469) alarms on from inside
      the daemon; we name the holding PID(s) for one-``kill`` recovery.
    * Port is NOT bound → the relaunched ``sac listen`` never bound
      (startup crash / bind failed). Point at the listen log.
    """
    if _probe_bound(host, port):
        holders = _resolve_pids(port)
        who = ", ".join(str(p) for p in holders) if holders else "unknown"
        return (
            f"ERROR: port {port} still held by PID {who} but {health_path} "
            f"never answered within {deadline_secs}s — the daemon is UP but "
            f"NOT SERVING (wedged on the socket). This is the silent-outage "
            f"mode `_lifecycle/_bind_watchdog.py` alarms on. Retry "
            f"`sac listen restart --force` to SIGKILL PID {who}."
        )
    return (
        f"ERROR: bind failed — nothing is listening on {host}:{port} after "
        f"relaunch and {health_path} never answered within {deadline_secs}s. "
        f"The new `sac listen` did not bind (startup crash / port grab race). "
        f"Check the listen log for the bind error."
    )
