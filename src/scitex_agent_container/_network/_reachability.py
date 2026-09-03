"""Cross-host a2a REACHABILITY — can this host's forwarder reach each peer?

WHY THIS EXISTS (measured 2026-09-02)
    A cross-host ``a2a_send`` leaves this host through ``sac listen``'s
    forwarder (:mod:`.._listen._node_channel_forwarders`): ssh into the
    peer's alias, then ``curl 127.0.0.1:<port>`` on the peer with that peer's
    bearer from ``peer-tokens/<host>.token``. Every agent's sidecar binds
    127.0.0.1, so that ssh leg is the ONLY transport that can work in
    production — and whether it works depends on three per-host facts
    nothing was checking: an ssh alias for the peer, a peer token for it,
    and an ssh that actually connects. On ``scitex-compute-01`` the first
    was missing (no ``config.yaml``, so the forwarder saw no peers) and every
    send to ``scitex-compute-03`` failed with ``All connection attempts
    failed`` until somebody tried one by hand.

    This module asks that question ROUTINELY, per peer, through the SAME
    leg — the same RESOLVER and the same TRANSPORT, both imported from the
    forwarder's side rather than re-implemented here:

    * the ssh alias for a host is whatever
      :func:`.._listen._node_channel_forwarders._resolve_ssh_peer` answers
      — config.yaml ``peers:`` UNION the scitex-dev host registry, the one
      callable the forwarder dials by. Review round 2 of PR #1285 found the
      first cut resolving through a look-alike (the merged map, while the
      forwarder on develop still read the RAW config block), which is
      exactly how a probe reports "reachable" for a peer the forwarder
      cannot route. The parity test in ``test__reachability.py`` pins the
      callable identity, not just the answer;
    * :func:`.._network._ssh_curl._get_via_ssh_curl` shares its ssh argv
      with the forwarder's POST, the bearer is the same ``read_peer_token``
      value, and the target is the peer's listen ``/v1/health`` — the
      loopback bind the forwarder must reach.

WHAT ``reachable`` MEANS, AND WHAT IT DOES NOT
    ``True`` means: from THIS host, ssh to the peer's alias worked, curl on
    the peer reached ``127.0.0.1:<listen port>/v1/health``, and a
    ``sac-listen`` answered 200. That is the transport the forwarder uses,
    end to end, with the bearer it would carry: ssh alias + tunnel + a
    listen that is up.

    It does NOT prove the bearer is VALID. ``/v1/health`` is a PUBLIC path
    on the listen (``BearerAuthMiddleware.PUBLIC_PATHS``), answered before
    the bearer is looked at, so a stale or wrong ``peer-tokens/<host>.token``
    still yields ``True`` here and a 401 on the forwarder's real
    ``message:send``. A missing bearer is still reported (as UNKNOWN),
    because the forwarder refuses to send without one; an authenticated
    probe path is the proposed next step (see the PR body). Nor does
    ``True`` say a particular AGENT's port is up on that peer — the
    forwarder targets the agent's a2a port, which the peer's listen
    reaches from its own loopback.

THREE VALUES, NEVER TWO
    See :mod:`._reachability_report`, which owns the row / report / exit-code
    shape. ``None`` is UNKNOWN (no alias, no peer token, or this host) and is
    never counted as reachable or unreachable; a pass where nothing was
    measurable exits :data:`EXIT_NOTHING_MEASURABLE`, not 0.

ONE DEFINITION OF "THIS HOST"
    A row is this machine when :func:`resolve_targets` says so — the
    ``local`` flag on :class:`Target`, decided against the caller's
    ``local_names`` (the CLI passes ``_local_host_names()``, which pivots
    through the host registry so ``DXP480TPLUS-994`` and ``scitex-nas-03``
    are one machine). That flag is CARRIED into the row and the report
    (:attr:`~._reachability_report.HostReachability.local`) so every
    consumer — the alarm above all — skips the self row by the same
    decision instead of re-deriving it from a name spelled differently.

CONSUMERS
    * ``sac a2a reachability`` renders a :class:`ReachabilityReport` and
      exits with :meth:`ReachabilityReport.exit_code`; the scitex-dev
      supervisor's ``PeriodicRunner`` persists that exit code per run in
      ``~/.scitex/dev/runtime/periodic-executions.jsonl``.
    * ``--record`` writes the report to :func:`default_report_path`
      (``runtime_root()/a2a-reachability.json``); ``sac a2a reachability
      --last`` reads it back through :func:`read_report`. That file is the
      full per-pass picture.
    * :mod:`._reachability_alarm` routes TRANSITIONS into sac's event log —
      the rail ``fleet-reconcile`` and ``host-sync-check`` already record to.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .._listen._config import DEFAULT_LISTEN_PORT

# THE forwarder's resolver — imported, never re-implemented, so the alias
# the probe dials and the alias a cross-host send dials cannot diverge.
from .._listen._node_channel_forwarders import _resolve_ssh_peer
from .._listen.peer_tokens import PeerTokenError, read_peer_token
from .._state._peer_resolve import peers_with_registry
from .._state.host_registry import registry_hosts
from ._reachability_report import (
    EXIT_ALL_REACHABLE,
    EXIT_NOTHING_MEASURABLE,
    EXIT_UNREACHABLE,
    REPORT_FILENAME,
    TRANSPORT_NONE,
    TRANSPORT_SSH,
    HostReachability,
    ReachabilityReport,
    default_report_path,
    exit_code_for,
    read_report,
    write_report,
)
from ._ssh_curl import _get_via_ssh_curl, split_status_line

__all__ = [
    "DEFAULT_LISTEN_PORT",
    "EXIT_ALL_REACHABLE",
    "EXIT_NOTHING_MEASURABLE",
    "EXIT_UNREACHABLE",
    "HEALTH_PATH",
    "LOCAL_HOST_REASON",
    "REPORT_FILENAME",
    "TRANSPORT_NONE",
    "TRANSPORT_SSH",
    "HostReachability",
    "ReachabilityReport",
    "Target",
    "default_report_path",
    "exit_code_for",
    "probe_target",
    "probe_targets",
    "read_report",
    "resolve_targets",
    "run_probe",
    "write_report",
]

#: The route probed on the peer's listen. PUBLIC — ``BearerAuthMiddleware``
#: answers it before looking at the bearer — which is why a 200 here proves
#: the TRANSPORT (alias + tunnel + listen up) and not the bearer's validity.
HEALTH_PATH = "/v1/health"

#: The value the listen's health body carries; anything else answering 200 on
#: the port is not the daemon the forwarder needs.
_EXPECTED_SERVICE = "sac-listen"

#: Why this machine's own row is UNKNOWN: there is no leg to probe.
LOCAL_HOST_REASON = (
    "this host — the forwarder never ssh-es to itself, so there is nothing to probe"
)


@dataclass(frozen=True)
class Target:
    """A host to probe, resolved but not yet dispatched to.

    ``ssh_alias`` and ``no_route_reason`` are the forwarder's own answer for
    this host (:func:`~.._listen._node_channel_forwarders._resolve_ssh_peer`):
    the alias it would dial, or — when there is none — the sentence its 502
    would carry saying why.
    """

    host: str
    ssh_alias: str | None
    local: bool
    no_route_reason: str = ""


def _known_host_names() -> list[str]:
    """Every host the fleet knows, sorted — the ENUMERATION half.

    The forwarder never enumerates (its destination comes from the
    ``instances`` row), so this is the probe's own question: config.yaml
    ``peers:`` UNION the host registry, read through the same
    :func:`.._state._peer_resolve.peers_with_registry` the resolver merges
    by, PLUS registry rows with no ``ssh_alias`` — those are not routes, so
    the merged map drops them, but they must SURFACE here as UNKNOWN rather
    than vanish. Glob peers (``spartan-*``) are templates, not hosts.
    """
    from .._state.host_config import load as _load_host_config

    names: set[str] = set()
    for name in peers_with_registry(dict(_load_host_config().peers)):
        if any(ch in name for ch in "*?["):
            continue
        names.add(name)
    for row in registry_hosts():
        names.add(row.name)
    return sorted(names)


def resolve_targets(
    *,
    local_names: Iterable[str],
    only: Iterable[str] | None = None,
) -> list[Target]:
    """Every host the fleet knows, with the alias the forwarder would dial.

    The alias is NOT derived here. Each name is put to
    :func:`~.._listen._node_channel_forwarders._resolve_ssh_peer` — the
    callable ``sac listen``'s cross-host forwarder resolves a destination
    with (config.yaml ``peers:`` UNION the scitex-dev host registry, config
    winning) — so the probe dials exactly what a send would dial, and a host
    the forwarder would refuse is listed with the forwarder's refusal reason
    as an UNKNOWN row rather than silently dropped.

    ``local_names`` is every spelling of THIS machine; the matching row is
    flagged ``local`` here, once, and that flag rides through the report.

    ``only`` restricts the answer to those names and FAILS LOUDLY on a name
    nothing declares — a typo that probed nothing and exited 3 would read
    as "the fleet is unknown" instead of "you misspelled a host".
    """
    known = _known_host_names()
    if only is not None:
        wanted = list(dict.fromkeys(only))
        missing = [name for name in wanted if name not in known]
        if missing:
            raise KeyError(
                f"unknown host(s) {missing}; known: " + (", ".join(known) or "(none)")
            )
        names = wanted
    else:
        names = known

    lower_local = {n.lower() for n in local_names if n}
    targets: list[Target] = []
    for name in names:
        route = _resolve_ssh_peer(name)
        targets.append(
            Target(
                host=name,
                ssh_alias=route.ssh_target or None,
                local=name.lower() in lower_local,
                no_route_reason=route.reason,
            )
        )
    return targets


def _unknown(target: Target, *, transport: str, error: str) -> HostReachability:
    return HostReachability(
        host=target.host,
        ssh_alias=target.ssh_alias,
        transport=transport,
        reachable=None,
        elapsed_ms=None,
        error=error,
        local=target.local,
    )


def probe_target(
    target: Target,
    *,
    port: int = DEFAULT_LISTEN_PORT,
    timeout_s: float = 10.0,
    tokens_dir: Path | None = None,
) -> HostReachability:
    """Probe ONE host over the forwarder's transport. Never raises.

    The order of refusals is the forwarder's own: this host → no leg; no
    alias → no leg; no peer token → the forwarder would refuse before
    dispatching; then the ssh+curl leg itself. Each refusal is UNKNOWN with
    the file to fix named; only a dispatched leg can yield ``True`` or
    ``False``. The local check comes FIRST and is not a shortcut: a local
    row with an alias and a token would otherwise ssh to itself.
    """
    if target.local:
        return _unknown(target, transport=TRANSPORT_NONE, error=LOCAL_HOST_REASON)
    if not target.ssh_alias:
        # The forwarder's own refusal sentence, so the operator reads here
        # exactly what a cross-host send to this host would 502 with.
        why = target.no_route_reason or (
            "the host registry (hosts.yaml) declares ssh_alias: null and "
            "config.yaml peers: does not name it"
        )
        return _unknown(
            target,
            transport=TRANSPORT_NONE,
            error=(
                f"no ssh alias for {target.host!r}: {why} — the forwarder cannot "
                "reach it either. Add `ssh_alias` to its hosts.yaml row or a "
                "peers: entry in config.yaml if it is reachable."
            ),
        )
    try:
        bearer = read_peer_token(peer_host=target.host, tokens_dir=tokens_dir)
    except PeerTokenError as exc:
        # The forwarder refuses a send with exactly this message (502), so a
        # missing token is a real gap — but it is UNKNOWN, not False: the
        # transport was never exercised.
        return _unknown(target, transport=TRANSPORT_SSH, error=str(exc))

    started = time.monotonic()
    try:
        rc, stdout, stderr = _get_via_ssh_curl(
            host=target.ssh_alias,
            port=port,
            path=HEALTH_PATH,
            bearer=bearer,
            timeout_s=timeout_s,
        )
    except ValueError as exc:
        return _unknown(target, transport=TRANSPORT_SSH, error=str(exc))
    elapsed_ms = int((time.monotonic() - started) * 1000)

    def measured(ok: bool, error: str | None) -> HostReachability:
        return HostReachability(
            host=target.host,
            ssh_alias=target.ssh_alias,
            transport=TRANSPORT_SSH,
            reachable=ok,
            elapsed_ms=elapsed_ms,
            error=error,
        )

    where = f"ssh://{target.ssh_alias} (→127.0.0.1:{port}{HEALTH_PATH})"
    if rc != 0:
        tail = stderr.decode("utf-8", errors="replace").strip()[-300:]
        return measured(False, f"{where} failed (rc={rc}): {tail or '(no stderr)'}")
    status, body = split_status_line(stdout)
    if status is None:
        return measured(
            False,
            f"{where}: curl printed no status line — stdout was "
            f"{body[:200]!r}; the remote shell did not run the probe as sent",
        )
    if status != 200:
        return measured(False, f"{where} answered HTTP {status}: {body[:200]}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    service = payload.get("service") if isinstance(payload, dict) else None
    if service != _EXPECTED_SERVICE:
        return measured(
            False,
            f"{where} answered 200 but not from {_EXPECTED_SERVICE}: {body[:200]!r}",
        )
    return measured(True, None)


def probe_targets(
    targets: Iterable[Target],
    *,
    port: int = DEFAULT_LISTEN_PORT,
    timeout_s: float = 10.0,
    tokens_dir: Path | None = None,
    max_workers: int = 8,
) -> list[HostReachability]:
    """Probe every target, in parallel, returning rows in target order.

    Parallel because the worst case is additive otherwise: an unreachable
    peer waits its ssh ``ConnectTimeout`` plus curl's ``--max-time``, and
    eight of those in series would outrun the scheduled job's own bound.
    """
    items = list(targets)
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(
                lambda t: probe_target(
                    t, port=port, timeout_s=timeout_s, tokens_dir=tokens_dir
                ),
                items,
            )
        )


def run_probe(
    *,
    local_names: Iterable[str],
    probed_from: str,
    only: Iterable[str] | None = None,
    port: int = DEFAULT_LISTEN_PORT,
    timeout_s: float = 10.0,
    tokens_dir: Path | None = None,
) -> ReachabilityReport:
    """Resolve, probe, and assemble one :class:`ReachabilityReport`.

    The peer map is not a parameter: the forwarder's resolver reads
    config.yaml and the registry itself, and handing it a different map
    here is how the two diverged in the first place.
    """
    started_at = datetime.now(tz=timezone.utc)
    t0 = time.monotonic()
    targets = resolve_targets(local_names=local_names, only=only)
    rows = probe_targets(targets, port=port, timeout_s=timeout_s, tokens_dir=tokens_dir)
    return ReachabilityReport(
        probed_from=probed_from,
        port=port,
        started_at_utc=started_at.isoformat(),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        rows=tuple(rows),
    )
