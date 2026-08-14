"""The fleet-listing VOCABULARY: host states, instruments, targets, reports.

``sac agents list`` used to show only what the CALLING host could see, so the
operator had to ssh machine-by-machine to find his own fleet — and, worse, a
listing taken from one host rendered every OTHER host as *nothing there*. This
module holds the words that stop that collapse; :mod:`._agent_list_fleet_probe`
takes the readings and :mod:`._agent_list_fleet` orchestrates them.

UNKNOWN IS NOT EMPTY (constitution §2)
-------------------------------------
A fan-out that silently drops an unreachable peer reports the same thing for
"that machine runs no agents" and "we could not ask that machine". Those are
different facts and only the first may be acted on. So every host we INTENDED
to query gets a :class:`HostReport` — including the ones that never answered —
and an unanswered report carries ``agents=None``, never ``0``:

* :data:`RESPONDED`   — the host answered; its rows are in the listing.
* :data:`TIMED_OUT`   — the probe RAN and hit the deadline. Distinct from
  UNREACHABLE on purpose: a host that is merely slow (or behind a wedged
  ProxyJump) has not refused us, and the two want different remedies.
* :data:`UNREACHABLE` — the probe RAN and positively failed (ssh non-zero).
* :data:`SAC_TOO_OLD` — we reached the host, sac is there, and it rejected an
  option this query REQUIRES for safety. Its own state because the retry that
  saves a merely-stale peer is forbidden here: ``sac accounts list`` sends
  ``--passive`` so the peer does not refresh (and thereby ROTATE) a credential
  every other host is still using, so re-asking without it would do the exact
  damage the flag prevents. Upgrading sac there is the remedy, and only a
  distinct state can say so.
* :data:`SAC_MISSING` — we REACHED the host and ``sac`` is not on its PATH.
  Measured live on two NAS boxes the first time this shipped. Folding it into
  UNREACHABLE would send the operator to debug a network that is fine; the
  remedy is to install sac there, and only a distinct state says so.
* :data:`MALFORMED`   — the host answered, but not with a listing we can read
  (an ancient sac, a shell banner on stdout, a truncated pipe). It is NOT
  unreachable — the transport demonstrably worked — and calling it so would
  send the operator to debug the wrong layer.
* :data:`NOT_QUERIED` — we never asked, because the fan-out was switched off.
  Also an unknown, and it earns a row for the same reason the others do: an
  operator who typed ``--host spartan`` and got silence must be told the host
  was not asked, not left to read the silence as "spartan is empty".

WHICH INSTRUMENT ANSWERED
-------------------------
Each report also names the SENSOR behind it, mirroring the
``evidence[].instrument`` vocabulary the per-agent liveness verdict already
publishes (:mod:`..._lifecycle._verdict_instruments`). It is deliberately a
SEPARATE vocabulary: that module's :class:`~..._lifecycle._verdict.Signal`
classifies instruments observing an AGENT's liveness against a closed
whitelist, and its destruction gate counts DISTINCT instruments. Letting a
host-transport reading borrow one of those names would let it pose as an
independent witness in that gate — the precise confusion the gate exists to
stop. Same discipline, separate axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

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
    "SAC_TOO_OLD",
    "TIMED_OUT",
    "UNREACHABLE",
    "UnknownHostFilter",
    "every_name",
    "fanout_suppressed_reason",
    "resolve_host_filter",
    "resolve_targets",
]

# --- host-reachability states (never a bool; see the module docstring) -----
RESPONDED = "responded"
TIMED_OUT = "timed_out"
UNREACHABLE = "unreachable"
SAC_MISSING = "sac_missing"
SAC_TOO_OLD = "sac_too_old"
MALFORMED = "malformed"
NOT_QUERIED = "not_queried"

# --- instruments: WHAT PHYSICALLY ANSWERED for this host -------------------
INSTRUMENT_LOCAL_REGISTRY = "local_registry"
INSTRUMENT_SSH = "ssh"
INSTRUMENT_NO_OBSERVATION = "no_observation"

# Bounded by default so one wedged ProxyJump can never hold the whole listing.
# 8s rather than build_ssh_argv's 10s ConnectTimeout: the remote leg also has to
# RUN ``sac agents list``, and an operator staring at a prompt would rather be
# told "spartan timed out" than wait.
DEFAULT_HOST_TIMEOUT_S = 8.0

_LOCALHOST_ALIASES = ("localhost", "local")


class UnknownHostFilter(ValueError):
    """``--host X`` named a host no route on this machine can reach."""


@dataclass(frozen=True)
class HostTarget:
    """One machine the listing intends to query.

    ``ssh`` is the DEDUPE key, not ``name``: this fleet reaches one NAS through
    two peer keys (``nas-03`` from config.yaml and ``scitex-nas-03`` from the
    scitex-dev registry) that render the identical ssh argv. Querying both would
    ssh twice and print every agent on that box twice. ``aliases`` keeps the
    collapsed names addressable, so ``--host scitex-nas-03`` still selects it.
    """

    name: str
    ssh: str
    local: bool = False
    aliases: tuple[str, ...] = ()

    def matches(self, wanted: str) -> bool:
        return wanted == self.name or wanted in self.aliases


@dataclass(frozen=True)
class HostReport:
    """What we learned about ONE host, and how we learned it."""

    host: str
    status: str
    instrument: str
    detail: str
    elapsed_ms: int = 0
    agents: int | None = None

    @property
    def responded(self) -> bool:
        return self.status == RESPONDED

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "status": self.status,
            "instrument": self.instrument,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
            # None, NEVER 0. A host that did not answer has an UNKNOWN agent
            # count; writing 0 would be the same lie as omitting the row.
            "agents": self.agents,
        }


@dataclass(frozen=True)
class FleetListing:
    """The merged rows plus the per-host reachability record behind them."""

    agents: list[dict] = field(default_factory=list)
    reports: list[HostReport] = field(default_factory=list)
    resolutions: tuple[tuple[str, str], ...] = ()
    suppressed_reason: str = ""
    # Peers IN SCOPE for this listing (i.e. after ``--host``), NOT the whole
    # topology: the header's "N peers NOT queried" note should describe what
    # this run meant to do, not enumerate machines the caller excluded.
    peers_known: int = 0

    @property
    def responded(self) -> int:
        return sum(1 for r in self.reports if r.responded)

    @property
    def total(self) -> int:
        return len(self.reports)

    @property
    def unanswered(self) -> list[HostReport]:
        return [r for r in self.reports if not r.responded]


def fanout_suppressed_reason(no_fanout: bool) -> str:
    """Why peers will not be queried, in words, or ``""`` when they will.

    The env form exists so a sandbox (this repo's own test suite, a laptop
    cron) can keep a listing local WITHOUT the caller having to remember a
    flag. Whichever way it is switched off, the header says so out loud — a
    quietly-local listing is the exact ambiguity this feature removes.
    """
    if no_fanout:
        return "--no-fanout"
    from ..._env import getenv as _sac_env

    raw = (_sac_env("AGENTS_LIST_NO_FANOUT") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return "SAC_AGENTS_LIST_NO_FANOUT"
    return ""


def _is_glob(name: str) -> bool:
    return any(ch in name for ch in "*?[")


def _load_peers() -> dict:
    # stx-allow: fallback (reason: a listing must still render THIS host when
    # the peer topology is missing or malformed — the header then says only one
    # host was known, which is honest, instead of crashing the command.)
    try:
        from ..._state._peer_resolve import peers_with_registry
        from ..._state.host_config import load as _load_host_config

        return dict(peers_with_registry(_load_host_config().peers))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return {}


def resolve_targets(
    peers: dict | None = None, local_host: str | None = None
) -> list[HostTarget]:
    """Every machine this host may query — local first, then its peers.

    Peers come from :func:`..._state._peer_resolve.peers_with_registry`, i.e.
    config.yaml UNION the scitex-dev host registry: the same set ``sac host
    list`` prints and ``sac host exec`` accepts, so the listing's reach is the
    topology the operator already declared rather than a second one invented
    here. Glob keys (``spartan-*``) are skipped — they are PATTERNS whose ssh
    target is synthesised per concrete node name, so there is no one machine to
    ask.
    """
    local = local_host or _resolve_display_host()
    if peers is None:
        peers = _load_peers()

    targets: list[HostTarget] = [HostTarget(name=local, ssh="", local=True)]
    by_ssh: dict[str, int] = {}
    for name, spec in peers.items():
        if _is_glob(name) or name == local:
            continue
        ssh = getattr(spec, "ssh", "") or ""
        if not ssh:
            continue
        seen = by_ssh.get(ssh)
        if seen is not None:
            prior = targets[seen]
            targets[seen] = HostTarget(
                name=prior.name,
                ssh=prior.ssh,
                local=prior.local,
                aliases=prior.aliases + (name,),
            )
            continue
        by_ssh[ssh] = len(targets)
        targets.append(HostTarget(name=name, ssh=ssh))
    return targets


def every_name(targets: Iterable[HostTarget]) -> set[str]:
    """Every name that ``--host`` will accept for ``targets``."""
    out: set[str] = set()
    for t in targets:
        out.add(t.name)
        out.update(t.aliases)
    return out


def resolve_host_filter(
    wanted: Sequence[str],
    targets: Sequence[HostTarget],
    local_host: str,
) -> tuple[list[HostTarget], tuple[tuple[str, str], ...]]:
    """Apply ``--host`` — exact match, with ``localhost`` resolved AT PARSE TIME.

    Returns ``(selected_targets, resolutions)`` where ``resolutions`` records
    every ``requested → resolved`` rewrite so the header can ECHO it.

    ``localhost`` / ``local`` name a DIFFERENT machine depending on where they
    are typed, which is exactly why ``spec.host: local`` is BANNED on the spec
    side ("placement must carry the RESOLVED hostname"). A live query filter may
    accept the word — the operator typing it knows which machine he is on — but
    the OUTPUT must never record the ambiguous claim. So it is resolved here and
    the resolution is SHOWN, not hidden.

    An unknown name raises :class:`UnknownHostFilter` naming every host that
    WOULD have worked. Quietly returning nothing would render "no such host"
    identically to "that host has no agents" — the same collapse again, one
    layer up.
    """
    if not wanted:
        return list(targets), ()
    selected: list[HostTarget] = []
    resolutions: list[tuple[str, str]] = []
    for raw in wanted:
        name = raw.strip()
        resolved = local_host if name.lower() in _LOCALHOST_ALIASES else name
        if resolved != name:
            resolutions.append((name, resolved))
        hit = next((t for t in targets if t.matches(resolved)), None)
        if hit is None:
            known = ", ".join(sorted(every_name(targets)))
            raise UnknownHostFilter(
                f"--host {raw!r} names no host this machine can reach"
                + (f" (resolved to {resolved!r})" if resolved != name else "")
                + f". Known hosts: {known}. See: sac host list"
            )
        if hit not in selected:
            selected.append(hit)
    return selected, tuple(resolutions)
