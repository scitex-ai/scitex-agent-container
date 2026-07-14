"""Orchestration for ``sac host sync`` — probe, decide, apply, VERIFY.

The order is the whole design:

1. **probe** (read-only)     — what code is actually on the peer?
2. **decide** (:mod:`._model`) — is that state safe to fast-forward?
3. **guard** (:mod:`._ci_guard`) — is CI using that checkout right now?
4. **apply** (:mod:`._apply`)  — one fast-forward, or nothing at all.
5. **verify** (re-probe)      — did the code we intended actually land?

Step 5 is not ceremony. "I verified" is a claim like any other, and a
green call is evidence the CALL RETURNED, not that the THING EXISTS: sac
has published nine tags that shipped nothing, and a ``.dist-info`` on
the centre still reports 0.21.11 next to current code. So the
verification never reads a version string. It asserts, on the peer,
that:

* HEAD is now EXACTLY the sha we aimed at,
* the interpreter that runs sac LOADS its code from inside that very
  checkout (this is what catches a wheel or a fossil ``.dist-info``
  shadowing the editable install — the failure mode that hid stale code
  for months), and
* a real SYMBOL imports out of it.

If any of those disagree, the sync reports FAILED. It does not report a
success it cannot substantiate.
"""

from __future__ import annotations

import enum
import subprocess
from dataclasses import dataclass

from .._state.host_config import Config, PeerSpec
from ._apply import FastForwardResult, apply_fast_forward
from ._ci_guard import DEFAULT_REPO, CiState, CiVerdict, check_ci_idle
from ._model import GraphState, PeerSyncReport, SyncDecision, sync_decision
from ._probe import probe_peer

__all__ = [
    "Outcome",
    "SyncResult",
    "exit_code_for",
    "check_peer",
    "sync_peer",
    "syncable_peers",
]


class Outcome(enum.Enum):
    """What happened — and therefore what the exit code must be."""

    #: Peer already matches the centre. A no-op — but still REPORTED.
    CURRENT = "current"
    #: Fast-forwarded and verified.
    SYNCED = "synced"
    #: ``--check`` found drift. Non-zero so a cron can alarm on it.
    DRIFTED = "drifted"
    #: Drift we will not touch (ahead / diverged / dirty), or CI is busy.
    REFUSED = "refused"
    #: We could not find out. Never conflated with "clean".
    UNDETERMINED = "undetermined"
    #: The fast-forward or its verification failed. The loudest state.
    FAILED = "failed"


_EXIT_CODES = {
    Outcome.CURRENT: 0,
    Outcome.SYNCED: 0,
    Outcome.DRIFTED: 1,
    Outcome.REFUSED: 1,
    Outcome.UNDETERMINED: 2,
    Outcome.FAILED: 2,
}


def exit_code_for(outcomes: list[Outcome]) -> int:
    """Worst outcome wins, so ``--all`` never hides one bad peer behind good ones."""
    return max((_EXIT_CODES[o] for o in outcomes), default=0)


@dataclass(frozen=True)
class SyncResult:
    """Everything that happened to one peer. All of it gets printed."""

    peer: str
    outcome: Outcome
    before: PeerSyncReport
    decision: SyncDecision | None = None
    ci: CiVerdict | None = None
    applied: FastForwardResult | None = None
    after: PeerSyncReport | None = None
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.CURRENT, Outcome.SYNCED)

    def to_dict(self) -> dict:
        return {
            "peer": self.peer,
            "outcome": self.outcome.value,
            "exit_code": _EXIT_CODES[self.outcome],
            "before": self.before.to_dict(),
            "after": self.after.to_dict() if self.after else None,
            "ci": self.ci.to_dict() if self.ci else None,
            "reason": self.decision.reason if self.decision else "",
            "applied": bool(self.applied and self.applied.ok),
            "notes": list(self.notes),
        }


def syncable_peers(cfg: Config) -> list[str]:
    """Peer names ``--all`` should visit.

    Excludes two kinds of entry, and says so rather than skipping quietly:

    * **glob patterns** (``spartan-*``) — templates for ephemeral compute
      nodes, not hosts. They have no durable checkout to reconcile.
    * **the centre itself** — you do not sync the brain to itself. The
      local host is where truth originates; its working tree is expected
      to carry branches and uncommitted work, and treating that as
      "remote drift" would alarm on every cron tick.
    """
    local = cfg.canonical_host()
    return sorted(
        name
        for name in cfg.peers
        if not any(ch in name for ch in "*?[") and name != local
    )


def check_peer(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    ref: str = "",
    timeout: int = 120,
    runner=subprocess.run,
) -> SyncResult:
    """Read-only: what is on ``peer``, and does it match the centre?

    Mutates NOTHING. This is what ``--check`` runs, and it is the half of
    the verb the operator asked for first: detection is the product.
    """
    before = probe_peer(peer, peers, ref=ref, timeout=timeout, runner=runner)
    if before.is_undetermined:
        outcome = Outcome.UNDETERMINED
    elif before.is_drifted:
        outcome = Outcome.DRIFTED
    else:
        outcome = Outcome.CURRENT
    return SyncResult(peer=peer, outcome=outcome, before=before)


def _verify(before: PeerSyncReport, after: PeerSyncReport) -> tuple[bool, list[str]]:
    """Did the code we intended actually land? Symbol + sha, never a version.

    Returns ``(ok, notes)``. Any disagreement is a hard failure — a sync
    that cannot substantiate its own result is a failed sync.
    """
    problems: list[str] = []
    notes: list[str] = []

    if after.is_undetermined:
        problems.append(
            f"post-sync probe could not read the peer back ({after.summary()}) — "
            "the fast-forward may have landed, but sac cannot vouch for it"
        )
        return False, problems

    if after.head != before.target_sha:
        problems.append(
            f"HEAD is {after.head or '?'} but we aimed at {before.target_sha} "
            f"({before.target}). The fast-forward did not land the intended code."
        )

    # The check that catches the failure nobody sees: the tree moved, but
    # the interpreter still loads sac from somewhere else entirely (a
    # wheel in site-packages, a fossil .dist-info). The version string
    # would happily report success here. The module PATH does not.
    if after.module and after.repo:
        if not (after.module + "/").startswith(after.repo.rstrip("/") + "/"):
            problems.append(
                f"the interpreter loads sac from {after.module}, which is OUTSIDE "
                f"the checkout we just synced ({after.repo}). The peer is running "
                "code this sync did not touch — most likely a wheel or a stale "
                ".dist-info shadowing the editable install."
            )

    if not after.symbol:
        problems.append(
            "the probed symbol did not import from the synced tree — the checkout "
            "is at the right sha but its code does not load"
        )

    if after.is_dirty:
        problems.append(
            f"the tree is dirty after the merge ({len(after.dirty_files)} file(s))"
        )

    if not problems and after.state is GraphState.BEHIND:
        notes.append(
            f"upstream advanced during the sync (now {after.behind} behind "
            f"{after.target}); the intended sha landed — re-run to catch up"
        )

    return (not problems), (problems or notes)


def sync_peer(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    ref: str = "",
    force: bool = False,
    repo: str = DEFAULT_REPO,
    timeout: int = 120,
    runner=subprocess.run,
) -> SyncResult:
    """Reconcile ``peer``'s sac checkout to ``ref``, or refuse and say why.

    One-way by construction: code moves centre → peer, never back. The
    only mutation possible is a fast-forward; ``AHEAD`` / ``DIVERGED`` /
    dirty peers are refused with their offending commits printed, and
    ``force`` does not unlock them (see :func:`.._model.sync_decision`).

    Args:
        peer: peer key from config.yaml.
        peers: parsed peers map.
        ref: target git ref; ``""`` = the peer's ``@{upstream}``.
        force: override the CI-idle guard ONLY, recording what it
            overrode so the override is never silent.
        repo: ``owner/name`` whose runners the CI guard inspects.
        timeout: per-ssh wall-clock cap.
        runner: injectable ``subprocess.run``-shaped callable.
    """
    before = probe_peer(peer, peers, ref=ref, timeout=timeout, runner=runner)
    decision = sync_decision(before, force=force)

    if not decision.allowed:
        if before.is_undetermined:
            outcome = Outcome.UNDETERMINED
        elif before.state is GraphState.CURRENT and not before.is_dirty:
            outcome = Outcome.CURRENT
        else:
            outcome = Outcome.REFUSED
        return SyncResult(peer=peer, outcome=outcome, before=before, decision=decision)

    # BEHIND + clean. Now: is CI using this very checkout right now?
    ci = check_ci_idle(peer, repo=repo, runner=runner)
    notes: list[str] = []
    if not ci.may_mutate:
        if force:
            notes.append(
                f"--force OVERRODE the CI guard: {ci.state.value} — {ci.detail}"
            )
        else:
            hint = (
                "Wait for CI to finish, or re-run with --force to override "
                "(it will print exactly what it overrides)."
                if ci.state is CiState.BUSY
                else "Fix gh access, or re-run with --force if you are certain "
                "CI is idle."
            )
            return SyncResult(
                peer=peer,
                outcome=(
                    Outcome.UNDETERMINED
                    if ci.state is CiState.UNKNOWN
                    else Outcome.REFUSED
                ),
                before=before,
                decision=SyncDecision(
                    allowed=False,
                    reason=(f"refusing to sync '{peer}': {ci.detail}\n  {hint}"),
                ),
                ci=ci,
            )

    applied = apply_fast_forward(
        peer,
        peers,
        repo=before.repo,
        ref=before.target,
        timeout=timeout,
        runner=runner,
    )
    if not applied.ok:
        return SyncResult(
            peer=peer,
            outcome=Outcome.FAILED,
            before=before,
            decision=decision,
            ci=ci,
            applied=applied,
            notes=tuple(notes),
        )

    after = probe_peer(peer, peers, ref=ref, timeout=timeout, runner=runner)
    verified, verify_notes = _verify(before, after)
    return SyncResult(
        peer=peer,
        outcome=Outcome.SYNCED if verified else Outcome.FAILED,
        before=before,
        decision=decision,
        ci=ci,
        applied=applied,
        after=after,
        notes=tuple(notes + verify_notes),
    )
