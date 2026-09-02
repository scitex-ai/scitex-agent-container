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
    transport: :func:`.._network._ssh_curl._get_via_ssh_curl` shares its
    ssh argv with the forwarder's POST, the bearer is the same
    ``read_peer_token`` value, and the target is the peer's listen
    ``/v1/health`` — the loopback bind the forwarder must reach.

WHAT ``reachable`` MEANS, AND WHAT IT DOES NOT
    ``True`` means: from THIS host, ssh to the peer's alias worked, curl on
    the peer reached ``127.0.0.1:<listen port>/v1/health``, and a
    ``sac-listen`` answered 200. That is the transport the forwarder uses,
    end to end, with the bearer it would carry.

    It does NOT say a particular AGENT's port is up on that peer (the
    forwarder targets the agent's a2a port, not the listen's), and — because
    ``/v1/health`` is a PUBLIC path on the listen — it does not prove the
    bearer would be ACCEPTED by an authenticated route. A missing bearer is
    still reported (as UNKNOWN), because the forwarder refuses to send
    without one.

THREE VALUES, NEVER TWO
    ``reachable`` is ``True`` / ``False`` / ``None``. ``None`` is UNKNOWN —
    the probe could not be run at all (no ssh alias in the registry, no
    peer token, or the host is this machine) — and it is NEVER counted as
    reachable and NEVER as unreachable. A pass in which every host is
    unknown measured nothing, and says so with its own exit code
    (:data:`EXIT_NOTHING_MEASURABLE`) rather than reporting "all clear".

CONSUMERS
    * ``sac a2a reachability`` renders a :class:`ReachabilityReport` and
      exits with :meth:`ReachabilityReport.exit_code`; the scitex-dev
      supervisor's ``PeriodicRunner`` persists that exit code per run in
      ``~/.scitex/dev/runtime/periodic-executions.jsonl``.
    * ``--record`` writes the report to :func:`default_report_path`
      (``runtime_root()/a2a-reachability.json``); ``sac a2a reachability
      --last`` reads it back through :func:`read_report`.
    * :mod:`._reachability_alarm` routes each row into sac's event log —
      the rail ``fleet-reconcile`` and ``host-sync-check`` already record to.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .._listen._config import DEFAULT_LISTEN_PORT
from .._listen.peer_tokens import PeerTokenError, read_peer_token
from .._state._peer_resolve import peers_with_registry
from .._state.host_registry import registry_hosts
from ._ssh_curl import _get_via_ssh_curl, split_status_line

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .._state.host_config import PeerSpec

__all__ = [
    "DEFAULT_LISTEN_PORT",
    "EXIT_ALL_REACHABLE",
    "EXIT_NOTHING_MEASURABLE",
    "EXIT_UNREACHABLE",
    "HEALTH_PATH",
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
    "write_report",
]

#: The route probed on the peer's listen. Public (no bearer needed to answer),
#: which is why a 200 here proves the TRANSPORT and not the bearer's validity.
HEALTH_PATH = "/v1/health"

#: The value the listen's health body carries; anything else answering 200 on
#: the port is not the daemon the forwarder needs.
_EXPECTED_SERVICE = "sac-listen"

#: ``transport`` values. ``ssh`` = the probe ran (or was refused only for a
#: missing token) over the forwarder's ssh leg; ``none`` = there is no leg to
#: run — no alias, or the host is this machine.
TRANSPORT_SSH = "ssh"
TRANSPORT_NONE = "none"

#: Exit codes. Documented in the verb's help; 2 is deliberately NOT used, it
#: stays Click's usage-error code and carries no domain meaning here.
EXIT_ALL_REACHABLE = 0
EXIT_UNREACHABLE = 1
EXIT_NOTHING_MEASURABLE = 3

#: Where ``--record`` lands the report, relative to sac's runtime root.
REPORT_FILENAME = "a2a-reachability.json"

#: Why this machine's own row is UNKNOWN: there is no leg to probe.
LOCAL_HOST_REASON = (
    "this host — the forwarder never ssh-es to itself, so there is nothing to probe"
)


@dataclass(frozen=True)
class HostReachability:
    """One host's verdict. The fixed answer shape ``--json`` emits per host.

    ``reachable`` is three-valued (see the module docstring); ``elapsed_ms``
    is ``None`` whenever nothing was dispatched; ``error`` is ``None`` only
    for ``reachable=True`` and otherwise names what stopped the probe, with
    the file or value to fix when there is one.
    """

    host: str
    ssh_alias: str | None
    transport: str
    reachable: bool | None
    elapsed_ms: int | None
    error: str | None

    def __post_init__(self) -> None:
        if self.transport not in (TRANSPORT_SSH, TRANSPORT_NONE):
            raise ValueError(
                f"HostReachability({self.host!r}).transport must be "
                f"{TRANSPORT_SSH!r} or {TRANSPORT_NONE!r}, got {self.transport!r}"
            )
        if self.reachable is not None and not isinstance(self.reachable, bool):
            raise ValueError(
                f"HostReachability({self.host!r}).reachable must be True, False "
                f"or None, got {self.reachable!r}"
            )
        if self.transport == TRANSPORT_NONE and self.reachable is not None:
            raise ValueError(
                f"HostReachability({self.host!r}): transport 'none' cannot carry "
                f"a measured verdict ({self.reachable!r}) — nothing was dispatched"
            )
        if self.reachable is None and not self.error:
            raise ValueError(
                f"HostReachability({self.host!r}): an UNKNOWN row must say why"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "ssh_alias": self.ssh_alias,
            "transport": self.transport,
            "reachable": self.reachable,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HostReachability":
        return cls(
            host=str(raw["host"]),
            ssh_alias=raw.get("ssh_alias"),
            transport=str(raw["transport"]),
            reachable=raw.get("reachable"),
            elapsed_ms=raw.get("elapsed_ms"),
            error=raw.get("error"),
        )


def exit_code_for(rows: Iterable[HostReachability]) -> int:
    """Map a pass's rows onto the three documented exit codes.

    * :data:`EXIT_UNREACHABLE` (1) — at least one host measured ``False``.
    * :data:`EXIT_NOTHING_MEASURABLE` (3) — no host measured anything: every
      row is UNKNOWN, or there are no rows. This is NOT success; a pass
      that could not look must not read as a pass that looked and found
      nothing wrong.
    * :data:`EXIT_ALL_REACHABLE` (0) — every MEASURED host is ``True``.
      UNKNOWN rows alongside measured ones do not turn 0 into 3: the
      measured hosts are still a real answer, and the unknown ones are
      listed by name in the report.
    """
    measured = [row.reachable for row in rows if row.reachable is not None]
    if not measured:
        return EXIT_NOTHING_MEASURABLE
    if any(value is False for value in measured):
        return EXIT_UNREACHABLE
    return EXIT_ALL_REACHABLE


@dataclass(frozen=True)
class ReachabilityReport:
    """The whole pass: every row plus where and when it was taken from."""

    probed_from: str
    port: int
    started_at_utc: str
    elapsed_ms: int
    rows: tuple[HostReachability, ...]

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.rows)

    def counts(self) -> dict[str, int]:
        return {
            "hosts": len(self.rows),
            "reachable": sum(1 for r in self.rows if r.reachable is True),
            "unreachable": sum(1 for r in self.rows if r.reachable is False),
            "unknown": sum(1 for r in self.rows if r.reachable is None),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "probed_from": self.probed_from,
            "port": self.port,
            "started_at_utc": self.started_at_utc,
            "elapsed_ms": self.elapsed_ms,
            "exit_code": self.exit_code,
            "counts": self.counts(),
            "hosts": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReachabilityReport":
        return cls(
            probed_from=str(raw["probed_from"]),
            port=int(raw["port"]),
            started_at_utc=str(raw["started_at_utc"]),
            elapsed_ms=int(raw["elapsed_ms"]),
            rows=tuple(HostReachability.from_dict(r) for r in raw.get("hosts", [])),
        )


@dataclass(frozen=True)
class Target:
    """A host to probe, resolved but not yet dispatched to."""

    host: str
    ssh_alias: str | None
    local: bool


def resolve_targets(
    *,
    peers: Mapping[str, "PeerSpec"],
    local_names: Iterable[str],
    only: Iterable[str] | None = None,
) -> list[Target]:
    """Every host the fleet knows, with the alias the forwarder would use.

    The alias comes from :func:`.._state._peer_resolve.peers_with_registry`
    — config.yaml ``peers:`` first, the scitex-dev host registry filling
    the gaps — which is the SSoT the CLI verbs already route by. Registry
    rows that declare NO ``ssh_alias`` are still listed (alias ``None``) so
    they surface as UNKNOWN rather than silently vanishing from the report;
    glob peers (``spartan-*``) are templates, not hosts, and are skipped.

    ``only`` restricts the answer to those names and FAILS LOUDLY on a name
    nothing declares — a typo that probed nothing and exited 3 would read
    as "the fleet is unknown" instead of "you misspelled a host".
    """
    merged = peers_with_registry(dict(peers))
    aliases: dict[str, str | None] = {}
    for name, spec in merged.items():
        if any(ch in name for ch in "*?["):
            continue
        aliases[name] = spec.ssh or None
    for row in registry_hosts():
        aliases.setdefault(row.name, row.ssh_alias or None)

    lower_local = {n.lower() for n in local_names if n}
    if only is not None:
        wanted = list(dict.fromkeys(only))
        missing = [name for name in wanted if name not in aliases]
        if missing:
            raise KeyError(
                f"unknown host(s) {missing}; known: "
                + (", ".join(sorted(aliases)) or "(none)")
            )
        names = wanted
    else:
        names = sorted(aliases)
    return [
        Target(host=name, ssh_alias=aliases[name], local=name.lower() in lower_local)
        for name in names
    ]


def _unknown(target: Target, *, transport: str, error: str) -> HostReachability:
    return HostReachability(
        host=target.host,
        ssh_alias=target.ssh_alias,
        transport=transport,
        reachable=None,
        elapsed_ms=None,
        error=error,
    )


def probe_target(
    target: Target,
    *,
    port: int = DEFAULT_LISTEN_PORT,
    timeout_s: float = 10.0,
    tokens_dir: Path | None = None,
) -> HostReachability:
    """Probe ONE host over the forwarder's transport. Never raises.

    The order of refusals is the forwarder's own: no alias → no leg; no
    peer token → the forwarder would refuse before dispatching; then the
    ssh+curl leg itself. Each refusal is UNKNOWN with the file to fix
    named; only a dispatched leg can yield ``True`` or ``False``.
    """
    if target.local:
        return _unknown(target, transport=TRANSPORT_NONE, error=LOCAL_HOST_REASON)
    if not target.ssh_alias:
        return _unknown(
            target,
            transport=TRANSPORT_NONE,
            error=(
                f"no ssh alias for {target.host!r}: not in config.yaml peers and "
                "the host registry (hosts.yaml) declares ssh_alias: null — "
                "inbound ssh is not possible, so the forwarder cannot reach it "
                "either. Add `ssh_alias` to its hosts.yaml row or a peers: "
                "entry in config.yaml if it is reachable."
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
    peers: Mapping[str, "PeerSpec"],
    local_names: Iterable[str],
    probed_from: str,
    only: Iterable[str] | None = None,
    port: int = DEFAULT_LISTEN_PORT,
    timeout_s: float = 10.0,
    tokens_dir: Path | None = None,
) -> ReachabilityReport:
    """Resolve, probe, and assemble one :class:`ReachabilityReport`."""
    started_at = datetime.now(tz=timezone.utc)
    t0 = time.monotonic()
    targets = resolve_targets(peers=peers, local_names=local_names, only=only)
    rows = probe_targets(targets, port=port, timeout_s=timeout_s, tokens_dir=tokens_dir)
    return ReachabilityReport(
        probed_from=probed_from,
        port=port,
        started_at_utc=started_at.isoformat(),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        rows=tuple(rows),
    )


def default_report_path() -> Path:
    """``runtime_root()/a2a-reachability.json`` — resolved per call."""
    from .._state.state_paths import runtime_root

    return runtime_root() / REPORT_FILENAME


def write_report(report: ReachabilityReport, *, path: Path | None = None) -> Path:
    """Persist ``report`` atomically (tmp + rename). Raises on failure.

    Deliberately NOT fail-open: ``--record`` is the scheduled job's whole
    reason to run, and a report that silently did not land would leave the
    consumer reading a stale one that says the fleet was fine.
    """
    target = Path(path) if path is not None else default_report_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def read_report(*, path: Path | None = None) -> ReachabilityReport | None:
    """The last recorded report, or ``None`` when none has been recorded.

    A file that exists but does not parse raises — a corrupt record is not
    "no record", and pretending otherwise would hide a broken writer.
    """
    target = Path(path) if path is not None else default_report_path()
    if not target.is_file():
        return None
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{target} does not hold a reachability report object")
    return ReachabilityReport.from_dict(raw)
