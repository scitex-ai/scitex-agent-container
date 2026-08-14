"""Fleet-wide agent listing — ask every host CONCURRENTLY, report all of them.

The orchestrator. The vocabulary (host states, instruments, targets, reports,
``--host`` resolution) lives in :mod:`._agent_list_fleet_model`; the two
instruments that take the readings live in :mod:`._agent_list_fleet_probe`.
Both are re-exported here so a caller has one import to reach for.

The one rule this module adds on top of theirs: **a host that could not be
reached never changes the exit code and is never dropped.** A listing that
exited non-zero on an unreachable peer would break every caller that parses it,
and a listing that omitted the peer would render *unknown* as *empty*. The
header carries the truth instead — see :mod:`._agent_list_fleet_render`.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout
from typing import Callable, Sequence

from ._agent_list_fleet_model import (  # noqa: F401 (re-export)
    DEFAULT_HOST_TIMEOUT_S,
    INSTRUMENT_LOCAL_REGISTRY,
    INSTRUMENT_NO_OBSERVATION,
    INSTRUMENT_SSH,
    MALFORMED,
    NOT_QUERIED,
    RESPONDED,
    SAC_MISSING,
    TIMED_OUT,
    UNREACHABLE,
    FleetListing,
    HostReport,
    HostTarget,
    UnknownHostFilter,
    every_name,
    fanout_suppressed_reason,
    resolve_host_filter,
    resolve_targets,
)
from ._agent_list_fleet_probe import local_probe, ssh_peer_probe  # noqa: F401
from ._agent_list_host import _resolve_display_host

__all__ = [
    "DEFAULT_HOST_TIMEOUT_S",
    "FleetListing",
    "HostReport",
    "HostTarget",
    "INSTRUMENT_LOCAL_REGISTRY",
    "INSTRUMENT_NO_OBSERVATION",
    "INSTRUMENT_SSH",
    "MALFORMED",
    "NOT_QUERIED",
    "RESPONDED",
    "SAC_MISSING",
    "TIMED_OUT",
    "UNREACHABLE",
    "UnknownHostFilter",
    "collect_fleet",
    "every_name",
    "fanout_suppressed_reason",
    "local_probe",
    "resolve_host_filter",
    "resolve_targets",
    "ssh_peer_probe",
]

# Enough workers that the whole fleet is in flight at once (this fleet has ~12
# hosts), capped so a pathological topology cannot spawn an unbounded pool.
_MAX_PARALLEL_HOSTS = 16

# Margin on the shared batch deadline, covering process teardown AFTER each
# probe's own inner subprocess timeout has already fired. Deliberately small
# and ADDITIVE rather than generous: a large fixed margin becomes the effective
# floor of the whole fan-out, so `--host-timeout 0.5` would still cost seconds.
_BATCH_MARGIN_S = 2.0


def _default_local_lister(registry, *, capability, machine, group, running_only):
    from ._agent_list import get_agent_list_data

    def lister() -> list[dict]:
        return get_agent_list_data(
            registry,
            capability=capability,
            machine=machine,
            group=group,
            running_only=running_only,
        )

    return lister


def collect_fleet(
    registry=None,
    *,
    capability: str | None = None,
    machine: str | None = None,
    group: str | None = None,
    running_only: bool = False,
    hosts: Sequence[str] = (),
    no_fanout: bool = False,
    host_timeout_s: float = DEFAULT_HOST_TIMEOUT_S,
    max_parallel_hosts: int = _MAX_PARALLEL_HOSTS,
    targets: Sequence[HostTarget] | None = None,
    local_lister: Callable[[], list[dict]] | None = None,
    peer_probe: Callable[..., tuple[HostReport, list[dict]]] | None = None,
) -> FleetListing:
    """Query every permitted host and return the merged rows + per-host reports.

    Wall-clock is bounded by the SLOWEST host, not by their sum: every peer is
    submitted BEFORE any result is collected, and the deadline is ONE shared
    budget rather than one restarted per future. That distinction is not
    theoretical — restarting the deadline per future makes n stalled peers cost
    ``ceil(n / workers) * T`` even though the pool is parallel, which is the
    exact defect ``_probe_remote_statuses`` and ``get_agent_list_data`` each had
    to fix in their own pools.

    ``targets`` / ``local_lister`` / ``peer_probe`` are injection seams so tests
    drive real callables instead of mocks, matching the ``remote_status_probe``
    / ``remote_run_ssh`` seams the sibling row builders already expose.
    """
    if local_lister is None:
        local_lister = _default_local_lister(
            registry,
            capability=capability,
            machine=machine,
            group=group,
            running_only=running_only,
        )

    local_host = _resolve_display_host()
    all_targets = (
        list(targets) if targets is not None else resolve_targets(local_host=local_host)
    )
    selected, resolutions = resolve_host_filter(hosts, all_targets, local_host)
    # Peers IN SCOPE for this listing, i.e. after ``--host``. Counted post-filter
    # so the header's "N peers NOT queried" note describes what this run meant to
    # do: an operator who asked for one host does not need to be told about nine
    # machines he deliberately excluded.
    peers_known = sum(1 for t in selected if not t.local)

    suppressed = fanout_suppressed_reason(no_fanout)
    skipped: list[HostTarget] = []
    if suppressed:
        skipped = [t for t in selected if not t.local]
        selected = [t for t in selected if t.local]

    reports: list[HostReport] = []
    rows: list[dict] = []
    for target in (t for t in selected if t.local):
        report, local_rows = local_probe(local_lister, target)
        reports.append(report)
        rows.extend(local_rows)

    # A host the caller NAMED and we then did not ask gets its own row. The
    # aggregate "N peers NOT queried" note in the header covers the unnamed
    # majority, but an operator who typed `--host spartan` and saw nothing must
    # be told spartan was not asked — otherwise the silence reads as "spartan
    # is empty", which is the whole failure mode this feature removes.
    if hosts:
        for target in skipped:
            reports.append(
                HostReport(
                    host=target.name,
                    status=NOT_QUERIED,
                    instrument=INSTRUMENT_NO_OBSERVATION,
                    detail=f"not queried ({suppressed})",
                )
            )

    remote_targets = [t for t in selected if not t.local]
    if remote_targets:
        probe = peer_probe or _default_peer_probe(
            capability=capability, machine=machine, group=group
        )
        report_rows = _fan_out(
            remote_targets,
            probe,
            host_timeout_s=host_timeout_s,
            max_parallel_hosts=max_parallel_hosts,
        )
        for report, peer_rows in report_rows:
            reports.append(report)
            rows.extend(peer_rows)

    return FleetListing(
        agents=rows,
        reports=reports,
        resolutions=resolutions,
        suppressed_reason=suppressed,
        peers_known=peers_known,
    )


def _default_peer_probe(*, capability, machine, group):
    def probe(target: HostTarget, timeout_s: float):
        return ssh_peer_probe(
            target,
            timeout_s,
            capability=capability,
            machine=machine,
            group=group,
        )

    return probe


def _fan_out(
    remote_targets: list[HostTarget],
    probe: Callable[..., tuple[HostReport, list[dict]]],
    *,
    host_timeout_s: float,
    max_parallel_hosts: int,
) -> list[tuple[HostReport, list[dict]]]:
    """Submit every peer, THEN collect against one shared deadline.

    ``shutdown(wait=False)`` rather than a ``with`` block: the context manager's
    ``__exit__`` joins every worker, which would defeat the very timeout this
    function exists to enforce (the same reason, and the same comment, as the
    local probe pool in ``_agent_list``).
    """
    out: list[tuple[HostReport, list[dict]]] = []
    workers = max(1, min(len(remote_targets), max_parallel_hosts))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(probe, target, host_timeout_s): target
            for target in remote_targets
        }
        deadline = time.monotonic() + host_timeout_s + _BATCH_MARGIN_S
        for future, target in futures.items():
            remaining = max(0.0, deadline - time.monotonic())
            # stx-allow: fallback (reason: a probe that never came back is a
            # TIMED_OUT host — a first-class answer, never an absent row.)
            try:
                out.append(future.result(timeout=remaining))
            except _FuturesTimeout:  # stx-allow: fallback (expected failure)
                future.cancel()
                out.append(
                    (
                        HostReport(
                            host=target.name,
                            status=TIMED_OUT,
                            instrument=INSTRUMENT_SSH,
                            detail=f"no answer within {host_timeout_s:g}s",
                        ),
                        [],
                    )
                )
            except Exception as exc:  # stx-allow: fallback (see inline comment)
                out.append(
                    (
                        HostReport(
                            host=target.name,
                            status=UNREACHABLE,
                            instrument=INSTRUMENT_NO_OBSERVATION,
                            detail=f"probe raised: {type(exc).__name__}: {exc}",
                        ),
                        [],
                    )
                )
    finally:
        pool.shutdown(wait=False)
    return out
