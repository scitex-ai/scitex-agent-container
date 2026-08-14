"""The two INSTRUMENTS behind a fleet listing: the local read and the ssh leg.

Split from :mod:`._agent_list_fleet` (512-line per-file cap) so the readings
live next to their vocabulary in :mod:`._agent_list_fleet_model` while
``collect_fleet`` stays a pure orchestrator.

WHY ``ssh`` AND NOT EACH PEER'S ``sac listen`` HTTP SURFACE
-----------------------------------------------------------
ADR-0015 already settled that question for the whole fleet: there is no overlay
net between these machines, a peer's listen binds ``127.0.0.1`` by default, and
nothing in ``config.yaml`` declares a peer's listen base URL (only the single
``lead:`` block does, for itself). That is why sac's own cross-host channel
forwarder runs over ssh + curl rather than plain HTTP. Asking the peer's OWN
``sac`` over ssh reaches the same state without inventing a second discovery
mechanism — and the report says ``instrument="ssh"`` so no reader has to guess
which rail answered.

The LOCAL host needs no transport at all, so it is read in-process and says so
(``instrument="local_registry"``). It deliberately does NOT proxy to this host's
own ``sac listen`` even when ``SAC_LISTEN_BASE_URL`` is set: the fleet view has
never proxied (``test_fleet_view_does_not_proxy`` pins that), and ``GET
/agents`` returns registry/comms-node rows in a different shape from the
enriched list rows — preferring it would silently change what the table means.

Every function here is TOTAL: a failure is a reported host STATE, never an
exception. An exception would drop the host from the listing, which is the
collapse this whole feature exists to prevent.
"""

from __future__ import annotations

import json as json_mod
import subprocess
import time
from typing import Callable

from ._agent_list_fleet_model import (
    INSTRUMENT_LOCAL_REGISTRY,
    INSTRUMENT_SSH,
    MALFORMED,
    RESPONDED,
    SAC_MISSING,
    TIMED_OUT,
    UNREACHABLE,
    HostReport,
    HostTarget,
)

__all__ = ["local_probe", "ssh_peer_probe"]

# The peer runs its OWN sac, which would fan out again — and its peers would fan
# out after that. ``--no-fanout`` is the recursion guard, and it is the same flag
# an operator can type to keep a listing local.
_REMOTE_BASE_ARGV = ("sac", "agents", "list", "--json")
_NO_FANOUT_ARGV = ("--no-fanout",)

# A peer running an OLDER sac rejects a flag it has never heard of. That refusal
# is proof it CANNOT recurse, so retrying without the guard is safe — and
# without the retry every not-yet-upgraded peer in the fleet would read
# UNREACHABLE, which is precisely the false negative this feature exists to
# kill. (sac hosts routinely run a stale build; the fleet has been bitten.)
_LEGACY_FLAG_MARKERS = ("no such option", "unrecognized arguments")


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0][:160] if stripped else ""


def _looks_like_unknown_flag(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _LEGACY_FLAG_MARKERS)


def _looks_like_missing_sac(rc: int, stderr: str) -> bool:
    """rc 127 (or the shell saying so) — we ARRIVED, sac was not there."""
    return rc == 127 or "command not found" in stderr.lower()


def _remote_argv(
    *, capability: str | None, machine: str | None, group: str | None, guard: bool
) -> list[str]:
    """The command run ON the peer.

    The label filters travel WITH the request so the peer applies them itself
    (one listing, filtered at the source) rather than shipping its whole roster
    back to be discarded here. ``-v`` / ``--all`` deliberately do NOT travel:
    those choose what the READER sees, and that decision belongs to the local
    render layer which holds every host's rows at once.
    """
    argv = list(_REMOTE_BASE_ARGV)
    if guard:
        argv += list(_NO_FANOUT_ARGV)
    if capability:
        argv += ["--capability", capability]
    if machine:
        argv += ["--machine", machine]
    if group:
        argv += ["--group", group]
    return argv


def _parse_rows(stdout: str) -> list[dict]:
    """Read a peer's ``--json`` payload. Raises ``ValueError`` when unreadable.

    Accepts BOTH shapes this codebase emits: the CLI's ``{"agents": [...]}``
    envelope and the bare list ``print_agent_list_json`` writes.
    """
    payload = json_mod.loads(stdout)
    rows = payload.get("agents") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("payload carries no 'agents' list")
    return [row for row in rows if isinstance(row, dict)]


def _stamp_host(rows: list[dict], target: HostTarget) -> list[dict]:
    """Rewrite each remote row's host so it names the MACHINE, not the peer's
    own point of view.

    A peer's ``sac agents list --json`` describes its agents as
    ``host="local"`` — true THERE, a lie HERE. Left alone, a fleet table would
    print a column of ``local`` and the operator would be back to ssh-ing host
    by host, which is the entire problem. The peer's ``host_display`` is its own
    resolved canonical name, so prefer that and fall back to the peer key we
    routed through.

    Rewriting ``host`` off the ``"local"`` sentinel also (correctly) exempts
    remote rows from ``_is_ghost_row``, which only ever means to hide a LOCAL
    registry row whose spec file is gone: a "File not found" measured HERE says
    nothing about a file on another machine.
    """
    out: list[dict] = []
    for row in rows:
        declared = row.get("host_display")
        host = (
            declared
            if isinstance(declared, str) and declared not in ("", "local", "localhost")
            else target.name
        )
        stamped = dict(row)
        stamped["host"] = host
        stamped["host_display"] = host
        out.append(stamped)
    return out


def local_probe(
    local_lister: Callable[[], list[dict]], target: HostTarget
) -> tuple[HostReport, list[dict]]:
    """Read THIS host in-process. Reported, never assumed."""
    started = time.monotonic()
    # stx-allow: fallback (reason: even the LOCAL host is reported rather than
    # assumed — a local read that blew up must not render as "no agents here".)
    try:
        rows = list(local_lister())
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return (
            HostReport(
                host=target.name,
                status=UNREACHABLE,
                instrument=INSTRUMENT_LOCAL_REGISTRY,
                detail=f"local read failed: {type(exc).__name__}: {exc}",
                elapsed_ms=_ms_since(started),
            ),
            [],
        )
    return (
        HostReport(
            host=target.name,
            status=RESPONDED,
            instrument=INSTRUMENT_LOCAL_REGISTRY,
            detail="read in-process; no transport needed",
            elapsed_ms=_ms_since(started),
            agents=len(rows),
        ),
        rows,
    )


def _peers_or_report(target: HostTarget):
    # stx-allow: fallback (reason: an unreadable peer topology is reported as an
    # unreachable host, not as an exception that kills the whole listing.)
    try:
        from ..._state._peer_resolve import peers_with_registry
        from ..._state.host_config import load as _load_host_config

        return peers_with_registry(_load_host_config().peers), None
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return None, HostReport(
            host=target.name,
            status=UNREACHABLE,
            instrument=INSTRUMENT_SSH,
            detail=f"peer topology unreadable: {type(exc).__name__}: {exc}",
        )


def ssh_peer_probe(
    target: HostTarget,
    timeout_s: float,
    *,
    capability: str | None = None,
    machine: str | None = None,
    group: str | None = None,
    peers: dict | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[HostReport, list[dict]]:
    """Ask ONE peer for its own listing over ssh. Returns ``(report, rows)``.

    Rides :func:`..._state.host_config.build_ssh_argv` — the fleet's single ssh
    choke point, which already renders ``via:`` ProxyJump chains, the Lmod
    ``env_preamble`` and the registry ``SCITEX_DIR`` pin — rather than
    hand-rolling an ssh command line beside it.

    Never raises. ``runner`` is the injection seam: a test drives the whole
    rc / timeout / legacy-peer mapping through a real callable, no mocks.
    """
    from ..._state.host_config import build_ssh_argv

    if peers is None:
        peers, failure = _peers_or_report(target)
        if failure is not None:
            return failure, []

    connect = int(max(2, min(timeout_s, 15)))
    started = time.monotonic()
    guard = True
    while True:
        argv = _remote_argv(
            capability=capability, machine=machine, group=group, guard=guard
        )
        # stx-allow: fallback (reason: every transport failure is a REPORTED
        # host state — an exception here would drop the host from the listing.)
        try:
            proc = runner(
                build_ssh_argv(
                    target.name,
                    argv,
                    peers,
                    extra_opts=["-o", f"ConnectTimeout={connect}"],
                ),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:  # stx-allow: fallback (expected)
            return (
                HostReport(
                    host=target.name,
                    status=TIMED_OUT,
                    instrument=INSTRUMENT_SSH,
                    detail=f"ssh timed out after {timeout_s:g}s",
                    elapsed_ms=_ms_since(started),
                ),
                [],
            )
        except Exception as exc:  # stx-allow: fallback (reason: see comment)
            return (
                HostReport(
                    host=target.name,
                    status=UNREACHABLE,
                    instrument=INSTRUMENT_SSH,
                    detail=f"ssh could not run: {type(exc).__name__}: {exc}",
                    elapsed_ms=_ms_since(started),
                ),
                [],
            )
        rc = int(getattr(proc, "returncode", 1) or 0)
        stderr = (getattr(proc, "stderr", "") or "").strip()
        if rc != 0 and guard and _looks_like_unknown_flag(stderr):
            guard = False  # older sac there: it cannot recurse, so ask again
            continue
        break

    if rc != 0:
        tail = f": {_first_line(stderr)}" if stderr else ""
        return (
            HostReport(
                host=target.name,
                # rc 127 is the SHELL saying it could not find `sac` — we got
                # all the way onto that machine. Measured live on two NAS boxes
                # the first time this shipped; calling it "unreachable" would
                # send the operator to debug a network that is fine.
                status=SAC_MISSING if _looks_like_missing_sac(rc, stderr) else UNREACHABLE,
                instrument=INSTRUMENT_SSH,
                detail=f"ssh exit {rc}{tail}",
                elapsed_ms=_ms_since(started),
            ),
            [],
        )
    # stx-allow: fallback (reason: a peer that answered with something we cannot
    # parse is MALFORMED, not unreachable — the transport demonstrably worked,
    # and saying "unreachable" sends the operator to debug the wrong layer.)
    try:
        rows = _stamp_host(_parse_rows(getattr(proc, "stdout", "") or ""), target)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return (
            HostReport(
                host=target.name,
                status=MALFORMED,
                instrument=INSTRUMENT_SSH,
                detail=f"answered, but the listing was unreadable ({exc})",
                elapsed_ms=_ms_since(started),
            ),
            [],
        )
    return (
        HostReport(
            host=target.name,
            status=RESPONDED,
            instrument=INSTRUMENT_SSH,
            detail="sac agents list --json over ssh",
            elapsed_ms=_ms_since(started),
            agents=len(rows),
        ),
        rows,
    )
