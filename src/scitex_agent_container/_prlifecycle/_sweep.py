"""JOB 1 — one board card per open PR, so the backlog is TRACKABLE.

Feeds facts to the board and stops there. scitex-todo's stale-active sweep
already owns nudging (see :mod:`._cards` for the SSOT split); this supplies the
cards it nudges about.

THE TRI-STATE, AND WHY IT IS THE POINT
--------------------------------------
:meth:`SweepOutcome.exit_code` returns 0 clean / 1 action needed / 2
could-not-determine, and the 2 is load-bearing.

Compare the bug it refuses to reproduce,
:meth:`.._authheal._pass.PassOutcome.exit_code`::

    if self.of(Verdict.BUDGET_UNKNOWN):
        return 2
    if self.of(Verdict.FAILED, ...):
        return 1
    return 0          # <-- "nothing observed" lands HERE, as SUCCESS

Because detection failing produces NO reports, and no reports fall through to
``return 0``, five systemd timers reported success every ten minutes while an
agent sat login-expired for hours.

The guard here is structurally different. It is not "did we find any problems"
— absence of problems is exactly what an unreadable fetch also looks like. It
is "did we PROVE we read the list" (:attr:`.._gh.PRFetch.readable`, a whitelist
of one state). ``EXIT_CLEAN`` is therefore only reachable AFTER that proof, and
any state we did not anticipate falls to UNKNOWN rather than to clean.

The same guard protects the destructive half. Completing the card of a PR that
"is no longer open" is inferred from ABSENCE — so on a blind pass every PR is
absent and every card would be completed at once, rendering a 35-PR backlog as
a clean board. :func:`sync_cards` therefore refuses to complete anything unless
the fetch is readable AND the board read succeeded.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ._cards import (
    complete_card,
    open_card_numbers,
    upsert_pr_card,
    upsert_sweep_heartbeat,
)
from ._gh import PRFetch, fetch_open_prs

__all__ = [
    "EXIT_ACTION",
    "EXIT_CLEAN",
    "EXIT_UNKNOWN",
    "RepoSweep",
    "SweepOutcome",
    "sync_cards",
]

#: 0 = the board matches reality, and we PROVED we could read reality.
EXIT_CLEAN = 0
#: 1 = something needs doing (a card write failed, or a dry-run found work).
EXIT_ACTION = 1
#: 2 = we could not determine the truth. NOT a failure to find problems —
#: a failure to LOOK. Never collapse this into 0.
EXIT_UNKNOWN = 2


@dataclass(frozen=True)
class RepoSweep:
    """What one repo's pass concluded and wrote."""

    repo: str
    fetch: PRFetch
    writes: tuple = ()
    #: ``None`` when the BOARD could not be read (so completion was skipped) —
    #: distinct from an empty dict, which means "no open cards".
    board_cards: Any = None

    @property
    def readable(self) -> bool:
        return self.fetch.readable

    def of(self, *actions: str) -> tuple:
        return tuple(w for w in self.writes if w.action in actions)


@dataclass(frozen=True)
class SweepOutcome:
    """Everything one sweep concluded, across every repo."""

    sweeps: tuple = ()
    applied: bool = False
    heartbeat_ok: bool = False
    unknown_detail: tuple = field(default_factory=tuple)

    @property
    def unreadable(self) -> tuple:
        """Repos whose PR list we could NOT read. The reason exit 2 exists."""
        return tuple(s for s in self.sweeps if not s.readable)

    def writes(self) -> tuple:
        return tuple(w for s in self.sweeps for w in s.writes)

    def counts(self) -> dict:
        out = {"upserted": 0, "completed": 0, "failed": 0, "would-write": 0}
        for write in self.writes():
            out[write.action] = out.get(write.action, 0) + 1
        out["repos-unreadable"] = len(self.unreadable)
        return {k: v for k, v in out.items() if v}

    def exit_code(self) -> int:
        """0 clean · 1 action needed · 2 COULD NOT DETERMINE.

        Read the order carefully — it is the whole design.

        The UNKNOWN check comes FIRST and is a positive test for *inability to
        look*, not for *absence of findings*. If even one repo's PR list was
        unreadable, this pass does not know the state of the world and says so,
        because a sweep that cannot see the backlog reporting SUCCESS is
        precisely how a 35-PR pile-up stays invisible until someone closes 31
        of them by hand.

        ``EXIT_CLEAN`` is unreachable until every repo produced
        ``FetchState.OK``. There is no fallthrough path to 0.
        """
        if not self.sweeps:
            # Asked to sweep nothing, or the repo list itself was unresolvable.
            # "I swept zero repos" is NOT "your board is clean" — it is a
            # configuration fact we cannot distinguish from a broken discovery,
            # so it is UNKNOWN by construction.
            return EXIT_UNKNOWN
        if self.unreadable:
            return EXIT_UNKNOWN
        if any(not s.readable for s in self.sweeps):
            # Belt AND braces: if `unreadable` were ever weakened, this second
            # positive test still refuses to reach EXIT_CLEAN while blind.
            return EXIT_UNKNOWN
        if any(w.action in ("failed", "would-write") for w in self.writes()):
            return EXIT_ACTION
        # Reached ONLY after proving every repo's list was read and parsed.
        return EXIT_CLEAN

    def summary(self) -> str:
        if self.unreadable:
            repos = ", ".join(s.repo for s in self.unreadable)
            return f"UNKNOWN — could not read the open-PR list for: {repos}"
        if not self.sweeps:
            return "UNKNOWN — no repo was swept, so nothing was determined"
        return " ".join(f"{k}={v}" for k, v in self.counts().items()) or "clean"


def sync_cards(
    repos,
    *,
    apply: bool = True,
    store=None,
    now: "datetime | None" = None,
    fetch=None,
    alarm: bool = True,
    err_stream: Any = None,
) -> SweepOutcome:
    """Upsert one card per open PR across ``repos``; complete cards whose PR is gone.

    ``apply=False`` is a REPORT: it fetches and decides, writing no per-PR card
    (only the heartbeat). Work it WOULD do is recorded as ``would-write``, which
    is why a dry-run with pending work exits 1.
    """
    stamp = now or datetime.now(timezone.utc)
    fetcher = fetch if fetch is not None else fetch_open_prs
    stream = err_stream if err_stream is not None else sys.stderr
    repos = tuple(repos)
    sweeps = []
    unknown_detail = []

    for repo in repos:
        result = fetcher(repo)
        if not result.readable:
            # LOUD, and no card is touched. A blind pass that quietly wrote
            # nothing would be indistinguishable from a healthy one.
            unknown_detail.append(f"{repo}: [{result.state.value}] {result.detail}")
            print(
                f"[pr-card-sweep] UNKNOWN for {repo}: [{result.state.value}] "
                f"{result.detail}\n"
                f"[pr-card-sweep]   NO card was created, updated or completed for "
                f"{repo}. An unreadable PR list is NOT an empty one, so this pass "
                f"refuses to conclude anything about it.",
                file=stream,
            )
            sweeps.append(RepoSweep(repo=repo, fetch=result))
            continue

        writes = []
        for pr in result.prs:
            if apply:
                writes.append(upsert_pr_card(pr, store=store, now=stamp))
            else:
                from ._cards import CardWrite, card_id_for

                writes.append(
                    CardWrite(
                        card_id_for(pr.repo, pr.number),
                        pr.number,
                        "would-write",
                        f"would upsert a card for {pr.repo}#{pr.number} "
                        f"({pr.age_days(stamp):.1f}d, {pr.author})",
                    )
                )

        # ---- completion: inferred from ABSENCE, so it is double-gated ------
        # `result.readable` is already proven here. The board read is the other
        # half: if we cannot list our own cards we cannot tell "this PR's card
        # is gone" from "we cannot see any cards", and completing on that guess
        # would wipe the board.
        board = open_card_numbers(repo, store=store)
        if board is None:
            print(
                f"[pr-card-sweep] {repo}: the BOARD could not be read — skipping "
                f"card completion this pass. Cards for merged/closed PRs stay "
                f"open rather than being guessed at.",
                file=stream,
            )
        else:
            still_open = result.numbers()
            for number, _card_id in sorted(board.items()):
                if number in still_open:
                    continue
                if apply:
                    writes.append(
                        complete_card(
                            repo,
                            number,
                            store=store,
                            reason=(
                                f"{repo}#{number} is no longer open (merged or "
                                f"closed) — completing its tracking card."
                            ),
                        )
                    )
                else:
                    from ._cards import CardWrite

                    writes.append(
                        CardWrite(
                            _card_id,
                            number,
                            "would-write",
                            f"would complete the card for {repo}#{number} "
                            f"(no longer open)",
                        )
                    )
        sweeps.append(
            RepoSweep(repo=repo, fetch=result, writes=tuple(writes), board_cards=board)
        )

    outcome = SweepOutcome(
        sweeps=tuple(sweeps), applied=apply, unknown_detail=tuple(unknown_detail)
    )
    heartbeat_ok = False
    if alarm:
        # The heartbeat runs LAST and reports the truth INCLUDING the unknowns,
        # so a board reader can see "the sweep ran but was blind" rather than
        # only "the sweep ran".
        heartbeat_ok = upsert_sweep_heartbeat(
            outcome.counts(),
            mode="apply" if apply else "check",
            repos=repos,
            detail=(
                "; ".join(unknown_detail)
                if unknown_detail
                else f"read {len(repos)} repo(s) cleanly"
            ),
            store=store,
            now=stamp,
            err_stream=err_stream,
        )
    return SweepOutcome(
        sweeps=tuple(sweeps),
        applied=apply,
        heartbeat_ok=heartbeat_ok,
        unknown_detail=tuple(unknown_detail),
    )
