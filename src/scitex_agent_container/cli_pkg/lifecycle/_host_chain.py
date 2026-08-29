#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``host:`` as a FALLBACK CHAIN — the resolver that actually honours it.

``config._types.HostsSpec`` has documented the list form as "priority order;
first available host wins (fallback chain)" since v3 shipped. Nothing
implemented it. Every site that reduced the list took ``host[0]`` and never
asked whether that host was usable, so a chain degraded exactly as well as a
string: not at all. On 2026-08-09 specs reverted to a single pinned host, sac
ssh-dispatched every lifecycle verb to it, the hop answered ``Permission
denied (publickey)``, and twelve agents went down with a documented fallback
mechanism sitting inert in the type.

This module is that mechanism. It is the ONE place a ``spec.host`` chain is
reduced to a route, and every reduction site in the control plane calls it.

Three-valued, on purpose
------------------------
Reachability is :data:`REACHABLE` / :data:`UNREACHABLE` / :data:`UNKNOWN`, and
the third value is never folded into either pole — that fold is the bug this
codebase ships most often (see ``_lifecycle/_verdict``'s "a probe that could
not run is UNKNOWN, never DEAD" and ``_listen/_reachability``'s "three states,
never two"). Concretely:

* :data:`UNREACHABLE` is EVIDENCE of failure — a probe ran and said no. Only
  evidence rejects a candidate.
* :data:`UNKNOWN` is the absence of evidence — no oracle was supplied, or the
  probe itself could not run. It rejects nothing.

The selection rule follows from that, and is one line: **take the first
candidate that has not been positively rejected.** A candidate is rejected
only by hard evidence — an :data:`UNREACHABLE` probe result, or a name that
resolves to neither this machine nor a registered peer. An :data:`UNKNOWN`
candidate still wins its position, because ``host:`` is a PRIORITY order and
demoting the operator's first choice on no evidence is just the opposite
collapse of UNKNOWN. That also makes the no-oracle case byte-identical to the
historical ``host[0]`` reduction, which is why the pure call sites (listings,
preflights) can adopt this resolver without probing anything.

A plain string is NOT a chain
-----------------------------
``host: <name>`` reduces exactly as it always has and is NEVER probed, even
when the caller supplies an oracle. There is nothing to fall back to, so a
probe could only convert a working dispatch into a refusal — a pure
regression. Pinned by test.

Purity
------
:func:`resolve_host_chain` never touches the network: reachability arrives as
an injected ``(host) -> verdict`` callable, exactly as ``peers`` and
``local_names`` already arrive at :func:`._common.classify_dispatch_host`.
:func:`ssh_reachability_oracle` builds the production callable and lives below
a loud divider; the resolver never calls it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from ._common import classify_dispatch_host

if TYPE_CHECKING:
    from ..._state.host_config import PeerSpec

# --- reachability verdicts (deliberately ternary; see module docstring) -----
REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"

#: ``(host) -> REACHABLE | UNREACHABLE | UNKNOWN``. Injected, never imported
#: by the resolver — the seam that keeps :func:`resolve_host_chain` pure.
ReachabilityOracle = Callable[[str], str]

# --- route kinds -----------------------------------------------------------
LOCAL = "local"
REMOTE = "remote"
UNROUTABLE = "unroutable"

# Placement kinds as returned by :func:`._common.classify_dispatch_host`.
_PLACEMENT_LOCAL = "local"
_PLACEMENT_REMOTE = "remote"
_PLACEMENT_UNKNOWN = "unknown"

#: Reachability of a candidate that was never probed (local entries, entries
#: past the winner, and every entry of a plain-string ``host:``).
NOT_PROBED = ""


@dataclass(frozen=True)
class HostCandidate:
    """One entry of a ``spec.host`` chain, with why it was taken or rejected.

    ``placement`` is the :func:`._common.classify_dispatch_host` verdict
    (``local`` / ``remote`` / ``unknown``); ``reachability`` is the oracle's
    answer, or :data:`NOT_PROBED` when the candidate was never probed (a local
    entry, an unroutable name, or any entry after the winner — the walk is
    lazy so a probe costs an ssh round-trip only when it can change the
    outcome).
    """

    host: str
    placement: str
    reachability: str = NOT_PROBED

    @property
    def rejected(self) -> bool:
        """True iff EVIDENCE disqualified this candidate.

        Only two things reject: a name that routes nowhere, and a probe that
        positively answered :data:`UNREACHABLE`. :data:`UNKNOWN` never
        rejects — that is the whole point of keeping it distinct.
        """
        return (
            self.placement == _PLACEMENT_UNKNOWN
            or self.reachability == UNREACHABLE
        )

    def describe(self) -> str:
        """One operator-readable line explaining this candidate's disposition."""
        if self.placement == _PLACEMENT_UNKNOWN:
            return (
                f"{self.host} — UNKNOWN HOST: neither this machine nor a "
                f"registered peer"
            )
        if self.placement == _PLACEMENT_LOCAL:
            return f"{self.host} — LOCAL: this machine"
        if self.reachability == UNREACHABLE:
            return (
                f"{self.host} — UNREACHABLE: registered peer, but the "
                f"reachability probe failed"
            )
        if self.reachability == REACHABLE:
            return f"{self.host} — REACHABLE: registered peer, probe succeeded"
        return (
            f"{self.host} — UNKNOWN REACHABILITY: registered peer, not probed "
            f"(no evidence against it)"
        )


@dataclass(frozen=True)
class HostChainRoute:
    """Where a ``spec.host`` chain resolved to, and the trail that got there.

    ``kind`` is :data:`LOCAL`, :data:`REMOTE` or :data:`UNROUTABLE`. ``peer``
    is set iff ``kind`` is :data:`REMOTE`. ``host`` is the winning chain entry
    (``None`` only for an empty / absent ``host:``). ``candidates`` records
    every entry the walk examined, in priority order, so the fail-loud path
    can name each one and why it was rejected.
    """

    kind: str
    peer: str | None = None
    host: str | None = None
    candidates: tuple[HostCandidate, ...] = field(default_factory=tuple)


def chain_hosts(spec_host: "str | Sequence[str] | None") -> list[str]:
    """Normalize ``spec.host`` to an ordered list of non-empty host names.

    The single shared normalizer — a string becomes a one-element list, a list
    is filtered of empties, and ``None`` / ``""`` becomes ``[]`` (unpinned).
    """
    if spec_host is None:
        return []
    if isinstance(spec_host, str):
        return [spec_host] if spec_host else []
    return [h for h in spec_host if h]


def is_remote_placement(
    spec_host: "str | Sequence[str] | None", current_host: str
) -> bool:
    """Will this placement land on a machine OTHER than ``current_host``?

    The peer-table-free question, for callers that must not pay an ssh probe
    or a config load to answer it (the ``--resume`` preflight). A chain that
    NAMES this machine anywhere is not remote — the resolver would take the
    local entry rather than dispatch, so a preflight that called it remote
    would be pre-flighting the wrong machine. For a plain string this is the
    identical comparison it has always been.
    """
    hosts = chain_hosts(spec_host)
    return bool(hosts) and current_host not in hosts


def resolve_host_chain(
    spec_host: "str | Sequence[str] | None",
    current_host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    local_names: Collection[str] = (),
    reachability: ReachabilityOracle | None = None,
) -> HostChainRoute:
    """Reduce a ``spec.host`` (string OR fallback chain) to one route.

    Pure — never raises, never logs, never reads files, never touches the
    network. ``peers`` / ``local_names`` / ``reachability`` all arrive from the
    caller, extending the seam :func:`._common.classify_dispatch_host` already
    established.

    Walk, in priority order:

    * a LOCAL entry wins immediately — we are that machine, so its availability
      needs no probe and an ssh hop to self is never rendered;
    * a REMOTE entry is probed (when an oracle was supplied); :data:`REACHABLE`
      and :data:`UNKNOWN` both win, :data:`UNREACHABLE` is skipped;
    * an entry naming neither this machine nor a peer is skipped;
    * if every entry was skipped the route is :data:`UNROUTABLE` — the caller
      fails loud with :func:`format_unroutable_chain_error`, never a silent
      local start.

    A plain STRING is classified exactly as before and never probed (see the
    module docstring); ``reachability`` is ignored for it entirely.

    An empty / absent ``host:`` is :data:`LOCAL` with no candidates.
    """
    hosts = chain_hosts(spec_host)
    if not hosts:
        return HostChainRoute(kind=LOCAL)

    # A bare string is not a fallback chain: no probing, ever.
    probe = None if isinstance(spec_host, str) else reachability

    examined: list[HostCandidate] = []
    for host in hosts:
        placement, peer = classify_dispatch_host(
            host, current_host, peers, local_names=local_names
        )
        if placement == _PLACEMENT_LOCAL:
            examined.append(HostCandidate(host, placement))
            return HostChainRoute(
                kind=LOCAL, host=host, candidates=tuple(examined)
            )
        if placement == _PLACEMENT_REMOTE and peer is not None:
            verdict = _probe(probe, host)
            examined.append(HostCandidate(host, placement, verdict))
            if verdict != UNREACHABLE:
                return HostChainRoute(
                    kind=REMOTE, peer=peer, host=host, candidates=tuple(examined)
                )
            continue
        examined.append(HostCandidate(host, _PLACEMENT_UNKNOWN))
    return HostChainRoute(kind=UNROUTABLE, candidates=tuple(examined))


def _probe(oracle: ReachabilityOracle | None, host: str) -> str:
    """Ask ``oracle`` about ``host``, degrading any surprise to :data:`UNKNOWN`.

    No oracle means no evidence, which is :data:`UNKNOWN` — NOT
    :data:`UNREACHABLE`. An oracle that raises or answers with anything outside
    the three verdicts is likewise "we did not learn anything", never a licence
    to reject a host the operator asked for.
    """
    if oracle is None:
        return UNKNOWN
    # stx-allow: fallback (reason: a broken reachability oracle must degrade to
    # "no evidence" — turning it into UNREACHABLE would let an oracle bug eject
    # a healthy host from the chain, the exact collapse this module forbids)
    try:
        verdict = oracle(host)
    except Exception:
        return UNKNOWN
    return verdict if verdict in (REACHABLE, UNREACHABLE, UNKNOWN) else UNKNOWN


def format_unroutable_chain_error(
    name: str,
    route: HostChainRoute,
    peers: Mapping[str, "PeerSpec"],
    *,
    verb: str = "start",
    current_host: str = "",
) -> str:
    """Actionable message for a chain in which EVERY candidate was rejected.

    Same shape and intent as :func:`._host_routing.format_unknown_host_error`
    (which it complements — that one explains a single bad name), but the chain
    case must account for every entry: an operator staring at ``host: [a, b,
    c]`` needs to know which of the three are typos and which are simply down,
    because the fixes differ. So every candidate is listed in priority order
    with its own reason.
    """
    peer_names = ", ".join(sorted(peers)) if peers else "(none registered)"
    lines = [
        f"Cannot {verb} agent {name!r}: every host in its `host:` fallback "
        f"chain was rejected.",
        "Chain (priority order):",
    ]
    for index, candidate in enumerate(route.candidates, start=1):
        lines.append(f"  {index}. {candidate.describe()}")
    if current_host:
        lines.append(f"This machine resolves as: {current_host}")
    lines.append(f"Registered peers: {peer_names}")
    lines.append(
        "  (from ~/.scitex/agent-container/config.yaml `peers:`; "
        "inspect with `sac host list`)"
    )
    lines.append("Fix ONE of:")
    lines.append(
        "  * bring one of the chain's hosts back up, then re-check with "
        "`sac host probe <peer>`,"
    )
    lines.append(
        "  * append this machine to the chain (`host: [..., "
        f"{current_host or '<this-host>'}]`) so the placement degrades locally "
        "instead of failing,"
    )
    lines.append(
        "  * correct a mistyped entry, or register it under `peers:` (glob "
        "patterns like `spartan-*:` with `via: [spartan]` cover "
        "login-node-fronted compute nodes),"
    )
    if verb == "start":
        lines.append(
            "  * or pass --no-redispatch to force a LOCAL start despite the pin."
        )
    else:
        lines.append(
            f"  * or run the verb on the owning host explicitly: "
            f"`sac --on <peer> agents {verb} {name}`."
        )
    return "\n".join(lines)


# ===========================================================================
# IMPURE — the production oracle. Nothing above this line calls it; callers
# that already own side effects (the dispatchers) wire it in explicitly.
# ===========================================================================


def ssh_reachability_oracle(
    peers: Mapping[str, "PeerSpec"],
    *,
    timeout: int = 5,
    runner: Callable[[list[str]], int] | None = None,
) -> ReachabilityOracle:
    """Build the real ``(host) -> verdict`` probe: one bounded ssh round-trip.

    The argv is rendered by :func:`.._state.host_config.build_ssh_argv` — sac's
    one canonical dispatch primitive — so a peer's ``via:`` ProxyJump chain and
    glob pattern apply and a two-tier HPC target is probed the same way it will
    be dispatched. A probe that agrees with the dispatch is the only kind worth
    having.

    Verdicts, mapped from the exit code of a remote ``true``:

    * rc 0 -> :data:`REACHABLE` — the hop completed and ran a command.
    * rc != 0 -> :data:`UNREACHABLE` — ssh reached a verdict and it was no
      (refused, auth failure, ``Permission denied (publickey)``: the 2026-08-09
      failure, which a chain must degrade past rather than die on).
    * ssh could not run at all, or the peer is unresolvable -> :data:`UNKNOWN`.
      We learned nothing, so we reject nothing.

    Answers are memoized per built oracle: one chain walk must not probe the
    same host twice, and a lifecycle verb is short enough that a cached answer
    cannot go stale within it.
    """
    cache: dict[str, str] = {}

    def _oracle(host: str) -> str:
        if host in cache:
            return cache[host]
        cache[host] = _ssh_probe(host, peers, timeout=timeout, runner=runner)
        return cache[host]

    return _oracle


def _ssh_probe(
    host: str,
    peers: Mapping[str, "PeerSpec"],
    *,
    timeout: int,
    runner: Callable[[list[str]], int] | None,
) -> str:
    """One ssh round-trip to ``host``; see :func:`ssh_reachability_oracle`."""
    # stx-allow: fallback (reason: an unresolvable peer / unbuildable argv means
    # we could not look, which is UNKNOWN — never a licence to eject the host)
    try:
        from ..._state.host_config import build_ssh_argv

        argv = build_ssh_argv(
            host,
            ["true"],
            peers,
            extra_opts=["-n", "-o", f"ConnectTimeout={timeout}"],
        )
    except Exception:
        return UNKNOWN
    # stx-allow: fallback (reason: ssh binary missing / probe timed out is "we
    # could not look" — UNKNOWN, never UNREACHABLE)
    try:
        rc = (runner or _run_rc)(argv)
    except Exception:
        return UNKNOWN
    if rc is None:
        return UNKNOWN
    return REACHABLE if rc == 0 else UNREACHABLE


def _run_rc(argv: list[str]) -> int | None:
    """Run ``argv``, return its exit code, or None when it could not run."""
    import subprocess

    # stx-allow: fallback (reason: could-not-run maps to UNKNOWN upstream)
    try:
        return subprocess.run(argv, capture_output=True, timeout=30).returncode
    except Exception:
        return None


__all__ = [
    "LOCAL",
    "NOT_PROBED",
    "REACHABLE",
    "REMOTE",
    "UNKNOWN",
    "UNREACHABLE",
    "UNROUTABLE",
    "HostCandidate",
    "HostChainRoute",
    "ReachabilityOracle",
    "chain_hosts",
    "format_unroutable_chain_error",
    "is_remote_placement",
    "resolve_host_chain",
    "ssh_reachability_oracle",
]
