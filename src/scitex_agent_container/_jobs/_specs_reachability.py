"""The cross-host a2a REACHABILITY probe, as a scheduled job.

Split into its own module like :mod:`._specs_liveness` and
:mod:`._specs_maintenance`: one operational concern a reader checks as a
unit. It REPORTS, never repairs — the remedy for an unreachable peer is a
config change (an ssh alias, a peer token) that a timer must not make.

WHY A TIMER, AND NOT A CHECK ON SEND
    The transport a cross-host ``a2a_send`` rides — ssh to the peer's alias,
    curl on the peer's loopback with the peer's bearer — depends on three
    per-host facts (alias, token, ssh connectivity) that drift SILENTLY:
    nothing fails until an agent actually sends, and when it does the sender
    sees ``All connection attempts failed`` with no indication which of the
    three is missing. MEASURED 2026-09-02: ``scitex-compute-01`` and
    ``scitex-compute-03`` had no ``config.yaml`` at all, so every cross-host
    send from them took a leg that cannot work in production. Nobody knew
    until a send was tried by hand. A probe that runs on its own beat
    reports the gap before an agent pays for it, and records it where the
    other unattended passes record (sac's event log).

CADENCE
    Every 15 minutes: an alias or token gap is a config fact that stays
    broken until fixed, so a faster tick buys only more identical records;
    slower, and a peer can be silently unreachable for most of an hour. A
    pass is one ssh+curl per peer, run in parallel, bounded per host by the
    verb's ``--timeout`` (10 s curl + 15 s ssh), so the 180 s command bound
    covers the whole fleet including every peer being down at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.jobs import JobSpec

__all__ = ["reachability_jobs"]


def reachability_jobs(*, executable: str | None = None) -> "list[JobSpec]":
    """The a2a-reachability JobSpec.

    ``executable`` is the same test seam :func:`._sac_bin.sac_bin` exposes,
    threaded through so a test can resolve the payload against a venv-shaped
    tree it built on disk rather than asserting an environmental fact.
    """
    from scitex_dev.jobs import JobSpec

    from ._sac_bin import sac_bin

    # ABSOLUTE, resolved per host -- see :mod:`._sac_bin` for the measurement.
    sac = sac_bin(executable=executable)

    return [
        JobSpec(
            name="scitex-agent-container-a2a-reachability",
            schedule="*/15 * * * *",  # every 15min (cron form; timer cadence below)
            # SELF-BOUNDING (180s). Peers are probed in parallel and each
            # leg is capped at 10s curl + 15s ssh connect, so a fleet where
            # EVERY peer is down still finishes well inside the bound; the
            # bound exists for the case where ssh itself hangs past its own
            # timeouts (a wedged ControlMaster), never for a legitimate run.
            command=(
                f"/usr/bin/timeout 180 {sac} a2a reachability --all --json --record"
            ),
            description=(
                "Probes the cross-host a2a transport from this host to every "
                "peer the fleet knows (config.yaml peers + the host registry): "
                "ssh to the peer's alias, curl 127.0.0.1:7878/v1/health on the "
                "peer with that peer's bearer — the SAME leg sac listen's "
                "forwarder takes for a cross-host send. Three-valued per host "
                "(reachable / unreachable / unknown — no alias, no peer token, "
                "or this host); unknown is never counted as reachable. Records "
                "each verdict in sac's event log (subsystem a2a-reachability) "
                "and the full report to runtime/a2a-reachability.json for "
                "`sac a2a reachability --last`. Exit 0 all reachable, 1 any "
                "unreachable, 3 nothing measurable. Mutates nothing on any peer."
            ),
            kind="timer",
            # 5min after boot: the listen and ssh agent have settled, and a
            # host that just came up is exactly the one whose peers want
            # checking. Then every 15min — see the module docstring.
            on_boot_sec="5min",
            on_unit_active_sec="15min",
        ),
    ]
