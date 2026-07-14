"""Verdict model for ``sac host sync`` — the one-way code channel.

The centre (ywata-note-win) is the BRAIN: code flows centre → remote and
a remote NEVER originates it. This module holds the vocabulary that
decision is made in.

The three-state rule (learned the hard way, twice)
-------------------------------------------------
A verdict here is never a boolean. ``UNREACHABLE`` is not "no drift",
and a probe that failed is not a peer that is clean. sac has shipped
this bug before — a ``pid <= 0`` read as "dead" destroyed live agents; a
probe timeout read as "DOWN" restarted a healthy daemon. So:

* **ALIVE / DEAD / UNKNOWN, never a pole.** Only a CORROBORATED verdict
  may authorise a mutation. :meth:`PeerSyncReport.is_undetermined` is
  the guard, and :func:`sync_decision` refuses on it.
* Absence of evidence is not evidence of cleanliness.

Why AHEAD is an ALARM and not a merge
-------------------------------------
If a remote holds commits the centre lacks, the one-way property has
ALREADY been violated — something wrote code on a machine that is not
allowed to originate it. Merging those commits back would RATIFY that
violation and make the remote a source of truth. Fast-forwarding or
resetting over them would DESTROY it, unexamined.

Neither is acceptable, so ``AHEAD`` / ``DIVERGED`` are terminal: STOP,
SHOUT, print the offending commits by subject line, and let a human
decide. A diverged remote is a BUG REPORT, not a branch to reconcile.

Two real incidents (2026-07-14) are exactly this shape, and both were
SILENT: an agent left a branch checked out in Spartan's sac tree — which
doubles as the CI runner's audit workspace — so a ``develop`` CI run
audited that branch while claiming to test develop; and Spartan's
``~/.scitex`` had been a symlink into an unrelated paper project for
weeks. Nothing announced either one. Hence: there is NO quiet success
path in this verb. A no-op still says what it verified.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

__all__ = [
    "GraphState",
    "PeerSyncReport",
    "SyncDecision",
    "sync_decision",
]


class GraphState(enum.Enum):
    """Where the peer's checkout sits relative to the centre's ref.

    Computed from the git OBJECT GRAPH (``rev-list --count``), never from
    mtimes: a plain ``git pull`` rewrites mtimes without changing content,
    and GPFS clock skew across hosts makes them unreliable. The object
    graph is content-addressed, exact, and clock-independent.

    * ``CURRENT`` — peer HEAD == the ref. Nothing to do (still reported).
    * ``BEHIND`` — the ref has commits the peer lacks. The peer is running
      STALE CODE. This is the one and only syncable state.
    * ``AHEAD`` — the peer has commits the ref lacks. ALARM: the one-way
      channel has been violated. Never merged, never discarded.
    * ``DIVERGED`` — both. The worst case: stale AND holding unpushed work.
    * ``NO_MODULE`` — ``scitex_agent_container`` is not importable there.
    * ``NOT_A_CHECKOUT`` — it imports, but not from a git working tree (a
      plain wheel install). There is no checkout to reconcile.
    * ``UNREACHABLE`` — ssh / fetch / probe failed. Drift is UNKNOWN, not
      absent. Never mutate on this.
    """

    CURRENT = "current"
    BEHIND = "behind"
    AHEAD = "ahead"
    DIVERGED = "diverged"
    NO_MODULE = "no-module"
    NOT_A_CHECKOUT = "not-a-checkout"
    UNREACHABLE = "unreachable"


# States in which we have NO trustworthy read of the peer's code. A
# mutation here would be acting on an unobserved negative — the exact
# false-RED that makes a repair tool more dangerous than the bug.
_UNDETERMINED = frozenset(
    {GraphState.NO_MODULE, GraphState.NOT_A_CHECKOUT, GraphState.UNREACHABLE}
)

# States that mean "the remote holds code the centre does not". Terminal.
_ALARM = frozenset({GraphState.AHEAD, GraphState.DIVERGED})


@dataclass(frozen=True)
class PeerSyncReport:
    """One peer's read-only verdict — the whole product of ``--check``.

    Every field is EVIDENCE, printed rather than summarised, because the
    operator's requirement is literally "I do not want it to be silent".

    Attributes:
        peer: peer key from config.yaml's ``peers:`` block.
        state: the :class:`GraphState` verdict.
        head: peer's HEAD sha (short), ``""`` when undetermined.
        target: the ref reconciled against (e.g. ``origin/develop``).
        target_sha: the ref's sha on the peer after fetching.
        ahead: commits the peer has that the ref lacks.
        behind: commits the ref has that the peer lacks.
        repo: the peer's checkout root, resolved by asking the peer's
            interpreter where it LOADS sac from — never by expanding a
            ``~`` locally (that yields the CENTRE's home; see
            :mod:`.._state.host_registry`).
        module: the peer's loaded ``scitex_agent_container.__file__``.
        symbol: the probed symbol's signature params (evidence the code
            is importable and what it actually exposes).
        dirty_files: uncommitted paths on the peer. Non-empty = REFUSE.
        ahead_commits: ``<sha> <subject>`` lines for each AHEAD commit —
            what a ``--force`` would be discarding. Printed before any
            decision, so nobody destroys work they never saw.
        detail: human-readable cause for the undetermined states.
    """

    peer: str
    state: GraphState
    head: str = ""
    target: str = ""
    target_sha: str = ""
    ahead: int = 0
    behind: int = 0
    repo: str = ""
    module: str = ""
    symbol: str = ""
    dirty_files: tuple[str, ...] = ()
    ahead_commits: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_undetermined(self) -> bool:
        """True when we have NO trustworthy read (never mutate on this)."""
        return self.state in _UNDETERMINED

    @property
    def is_dirty(self) -> bool:
        return bool(self.dirty_files)

    @property
    def is_drifted(self) -> bool:
        """True when the peer differs from the centre in ANY way.

        Drives ``--check``'s non-zero exit, so a cron can alarm on it.
        Deliberately includes ``dirty``: an uncommitted edit on a remote
        is drift from the centre's truth even though the object graph
        agrees. It is NOT "undetermined" — an undetermined peer is
        reported separately, because "I could not look" must never be
        rendered as "I looked and it was fine".
        """
        return self.state in (
            GraphState.BEHIND,
            GraphState.AHEAD,
            GraphState.DIVERGED,
        ) or bool(self.dirty_files)

    def summary(self) -> str:
        """One-line human verdict."""
        if self.state is GraphState.UNREACHABLE:
            return f"unreachable — {self.detail}" if self.detail else "unreachable"
        if self.state is GraphState.NO_MODULE:
            return "sac not importable on peer" + (
                f" — {self.detail}" if self.detail else ""
            )
        if self.state is GraphState.NOT_A_CHECKOUT:
            return f"not a git checkout (wheel install at {self.module or '?'})"
        bits: list[str] = []
        if self.state is GraphState.CURRENT:
            bits.append(f"current with {self.target}")
        elif self.state is GraphState.BEHIND:
            bits.append(f"{self.behind} behind {self.target} (STALE CODE)")
        elif self.state is GraphState.AHEAD:
            bits.append(
                f"{self.ahead} AHEAD of {self.target} (one-way channel violated)"
            )
        else:
            bits.append(
                f"DIVERGED: {self.ahead} ahead / {self.behind} behind {self.target}"
            )
        if self.dirty_files:
            bits.append(f"{len(self.dirty_files)} uncommitted file(s)")
        return "; ".join(bits)

    def to_dict(self) -> dict:
        """JSON projection for ``--json`` (cron / alarm consumers)."""
        return {
            "peer": self.peer,
            "state": self.state.value,
            "head": self.head,
            "target": self.target,
            "target_sha": self.target_sha,
            "ahead": self.ahead,
            "behind": self.behind,
            "repo": self.repo,
            "module": self.module,
            "symbol": self.symbol,
            "dirty_files": list(self.dirty_files),
            "ahead_commits": list(self.ahead_commits),
            "detail": self.detail,
            "drifted": self.is_drifted,
            "undetermined": self.is_undetermined,
        }


@dataclass(frozen=True)
class SyncDecision:
    """May we mutate this peer, and if not, exactly why not.

    ``reason`` is written for a human at 3am: it names what is wrong AND
    the next command to run. "On failure, give actionable hints."
    """

    allowed: bool
    reason: str = ""
    overrides: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.allowed


def sync_decision(report: PeerSyncReport, *, force: bool = False) -> SyncDecision:
    """Decide whether ``report`` authorises a mutating fast-forward.

    The ONLY green light is: a determined verdict, a clean tree, and
    ``BEHIND``. Everything else refuses, loudly and specifically.

    ``force`` is deliberately NARROW. It does NOT unlock the destructive
    refusals (``AHEAD`` / ``DIVERGED`` / dirty) — there is no
    non-destructive way to satisfy those, and the fast-forward-only
    invariant is precisely what makes this verb safe to run unattended
    from a cron. sac will not silently destroy a commit or an
    uncommitted edit that a human has never seen; it prints them and
    stops. (``force`` overrides the CI-idle guard only — see
    :mod:`._ci_guard` — because that is a SCHEDULING guard, not a
    data-safety one.)
    """
    if report.is_undetermined:
        return SyncDecision(
            allowed=False,
            reason=(
                f"refusing to sync '{report.peer}': its code state is UNKNOWN "
                f"({report.summary()}). An unknown peer is not a clean peer — "
                "sac never mutates on an unobserved negative. Fix reachability "
                f"first:  sac host probe {report.peer}"
            ),
        )
    if report.is_dirty:
        listing = "\n".join(f"    {f}" for f in report.dirty_files)
        return SyncDecision(
            allowed=False,
            reason=(
                f"refusing to sync '{report.peer}': the remote tree has "
                f"{len(report.dirty_files)} UNCOMMITTED file(s). sac never "
                "stashes and never discards work it did not write:\n"
                f"{listing}\n"
                f"  Inspect:  sac host exec {report.peer} -- "
                f"git -C {report.repo} status\n"
                "  Then commit, revert, or remove them ON THE PEER, and re-run."
            ),
        )
    if report.state in _ALARM:
        listing = "\n".join(f"    {c}" for c in report.ahead_commits) or "    (none)"
        return SyncDecision(
            allowed=False,
            reason=(
                f"REFUSING to sync '{report.peer}': it is {report.ahead} commit(s) "
                f"AHEAD of {report.target}. The remote holds code the centre does "
                "not — the one-way channel has already been violated. This is a "
                "BUG REPORT, not a branch to reconcile.\n"
                f"  Commits only on the peer:\n{listing}\n"
                "  sac will NOT merge them back (that would make the remote a "
                "source of truth) and will NOT discard them (that would destroy "
                "them). A human must decide:\n"
                f"    keep:     sac host exec {report.peer} -- "
                f"git -C {report.repo} push origin HEAD:<branch>\n"
                f"    discard:  sac host exec {report.peer} -- "
                f"git -C {report.repo} reset --hard {report.target}"
            ),
        )
    if report.state is GraphState.CURRENT:
        return SyncDecision(
            allowed=False,
            reason=f"'{report.peer}' is already current with {report.target}",
        )
    return SyncDecision(allowed=True, overrides=("ci-busy",) if force else ())
