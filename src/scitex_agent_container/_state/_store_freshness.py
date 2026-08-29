"""Notice at BOOT that the store you resolved is not the one you were using.

2026-08-08, measured. A dotfiles deploy replaced this agent's `~/.scitex` — its
LIVE STATE ROOT, credentials included — with a symlink into a git worktree, and
moved the real tree aside as `.scitex_back_<timestamp>`. The agent then booted,
resolved its message store inside the substituted tree, found 149 rows whose
newest was a MONTH OLD, and carried on reporting healthy.

Nothing was broken in a way anything checked for. The store existed, opened,
parsed, and answered queries. It was simply not the store that had been in use
an hour earlier, and no component asked the one question that would have said
so: does the newest row in here look like it belongs to a store this process
has been writing to?

The operator found out four hours later by asking me something I should have
known and getting a blank. His words: 「忘れている、思い出せない、となると結構
辛いです。」

This module is that question, as a function. It is deliberately about TIME
rather than about paths or storage engines: a path check would have to know
about symlinks, overlays, binds and worktrees and would still miss the next
mechanism. "The newest thing in my store is far older than my own start" is
true of every one of those failures and needs to know about none of them.

THE THRESHOLD IS A JUDGEMENT, and the default is deliberately generous. A quiet
agent legitimately has an old newest-row — it may not have been spoken to for
days — so this must not cry wolf at every idle boot. What it catches is the
qualitatively different case: a store whose newest row predates the process by
far more than the agent has plausibly been idle. Callers that know their own
cadence should pass their own threshold.

Three-valued, and here the unknown is the common case rather than an edge: a
brand-new store has no rows at all, and that is neither fresh nor stale.
Reporting it as stale would make every first boot shout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CODE_EMPTY",
    "CODE_FRESH",
    "CODE_FUTURE",
    "CODE_STALE",
    "CODE_UNKNOWN",
    "DEFAULT_STALE_AFTER_S",
    "FreshnessVerdict",
    "assess_store_freshness",
]

#: 24 hours. An agent can plausibly sit idle overnight; a store whose newest row
#: predates this boot by more than a day, when the agent was demonstrably active
#: within it, is the shape of a substituted store rather than a quiet one.
DEFAULT_STALE_AFTER_S: Final = 86_400.0

CODE_FRESH: Final = 200
CODE_EMPTY: Final = 204
CODE_STALE: Final = 409
CODE_FUTURE: Final = 412
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class FreshnessVerdict:
    """Whether the resolved store plausibly belongs to this process.

    ``fresh`` is three-valued: ``True`` plausible, ``False`` suspicious,
    ``None`` cannot tell. Defines no ``__bool__`` so ``if verdict:`` cannot read
    as an all-clear for a suspicious store.
    """

    fresh: bool | None
    code: int
    reason: str
    age_s: float | None = None

    def __post_init__(self) -> None:
        if self.fresh not in (True, False, None):
            raise ValueError(
                f"FreshnessVerdict.fresh must be True/False/None, got {self.fresh!r}"
            )
        if not self.reason:
            raise ValueError("FreshnessVerdict.reason must be non-empty")
        if self.fresh is True and self.code not in (CODE_FRESH, CODE_EMPTY):
            raise ValueError(
                f"FreshnessVerdict: fresh=True must carry CODE_FRESH or CODE_EMPTY, got {self.code}"
            )
        if self.fresh is None and self.code != CODE_UNKNOWN:
            raise ValueError(
                f"FreshnessVerdict: fresh=None must carry CODE_UNKNOWN, got {self.code}"
            )


def assess_store_freshness(
    *,
    newest_row_ts: float | None,
    process_started_at: float,
    store_label: str,
    had_rows: bool | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> FreshnessVerdict:
    """Does the newest row in this store look like it belongs here?

    ``newest_row_ts`` is None when the store is EMPTY or was not read. Those are
    different, and ``had_rows`` separates them:

        had_rows=False  -> genuinely empty; a first boot, not a fault
        had_rows=True   -> rows exist but their timestamp could not be read: UNKNOWN
        had_rows=None   -> the store was not inspected at all: UNKNOWN

    An empty store passes rather than warns, deliberately: a new agent's first
    boot must not shout, and a store that is empty when it should not be is a
    different check (one that knows what the agent expects to find).
    """
    if newest_row_ts is None:
        if had_rows is False:
            return FreshnessVerdict(
                fresh=True,
                code=CODE_EMPTY,
                reason=f"{store_label} is empty — nothing to compare, which is normal on a first boot",
            )
        return FreshnessVerdict(
            fresh=None,
            code=CODE_UNKNOWN,
            reason=(
                f"could not read the newest row timestamp from {store_label}; "
                "a store that cannot answer this is not a store known to be healthy"
            ),
        )

    age = process_started_at - newest_row_ts
    if age < 0:
        return FreshnessVerdict(
            fresh=False,
            code=CODE_FUTURE,
            reason=(
                f"{store_label} contains a row {abs(age):.0f}s in the FUTURE relative to this "
                "process's start — either a clock disagreement or another writer, and both "
                "mean this process is not the only one here"
            ),
            age_s=age,
        )
    if age > stale_after_s:
        return FreshnessVerdict(
            fresh=False,
            code=CODE_STALE,
            reason=(
                f"{store_label}: newest row is {age / 3600.0:.1f}h older than this process's start. "
                "That is the shape of a SUBSTITUTED store — a moved-aside state root, a fresh "
                "overlay, a deploy that symlinked the parent away — not of a quiet agent. "
                "Check what the path actually resolves to (`readlink -f`) before trusting it"
            ),
            age_s=age,
        )
    return FreshnessVerdict(
        fresh=True,
        code=CODE_FRESH,
        reason=f"{store_label}: newest row is {age / 3600.0:.1f}h before this process's start",
        age_s=age,
    )
