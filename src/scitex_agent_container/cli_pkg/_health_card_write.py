#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/cli_pkg/_health_card_write.py
"""BEHAVIOURAL probe: can this image actually record work on a real card?

Every card-store gate we own asserts PRESENCE. This one asserts BEHAVIOUR, and
it exists because presence was measured three times on a broken artifact and
passed three times.

INCIDENT 2026-08-23. scitex-cards 0.49.0 shipped `_merge_unseen_comment_rows`
to fix a defect where a card write DELETED comment rows the caller's stale
`card_json` had not seen. The new function indexes its rows POSITIONALLY::

    _mirror_rows.py:194   key = _key(row[0], row[1], row[2], row[3])

The package's own `_store_backend.py` states the rule that breaks::

    BY COLUMN NAME, not position. sqlite3.Row accepts both r["id"] and r[0];
    psycopg's dict_row accepts only the former and raises

The fleet store is PostgreSQL, so `row[0]` looks up the integer KEY 0 and
raises `KeyError: 0`. The package's own tests passed because they run on
SQLite, where `sqlite3.Row` accepts both forms.

WHAT MADE IT INVISIBLE, AND WHY THIS MODULE IS SHAPED THE WAY IT IS
Three independent gates went green on that artifact inside one hour:

  * the bake's in-image symbol probe   (`import _merge_unseen_comment_rows`)
  * the master-side SYMBOL_PROBE at `_remote_bake_core.py:421`
  * the Spartan bake's own content check ("present and correct")

All three ask "is the name there?". The name WAS there. The function was
present and broken, so a fourth presence check in a fourth location would have
been the same miss again. VERIFYING THE ARTIFACT CONTAINS THE FIX IS NOT
VERIFYING THE FIX WORKS: a symbol probe is strong against a LYING VERSION
STRING and blind to a WORKING NAME. Those are different failures.

THE SECOND COMMENT IS THE ENTIRE TEST
The merge returns early when a card holds no prior comments, so the broken
build behaves like this::

    fresh card, comment #1  (0 prior rows)  ->  OK
    fresh card, comment #2  (1 prior row)   ->  KeyError: 0

A smoke test that comments ONCE passes on the broken build. That is not a
detail to be careful about; it is the property the probe is built around, and
it is why `_SECOND_COMMENT` is named rather than being a loop index.

WHY NOT IN THE IMAGE BAKE
There is no live store at build time, and this assertion is meaningless
against anything but the real backend — the whole defect is a backend
disagreement (`sqlite3.Row` vs `dict_row`). So this is a POST-DEPLOY check:
run it after a swap, at agent boot, or by hand before trusting an image.

HONEST-UNKNOWN, following `_mcp/_healthcheck.py`
An unreachable store, an absent package, a refused connection — none of those
are evidence the write path WORKS, and none are evidence it is BROKEN. They
return ``unknown``. A false OK here would mask exactly the failure the module
exists to catch, so it is refused categorically. The three states are
distinguished by their own field; nothing collapses ``unknown`` into a pole.

THE PROBE WRITES, DELIBERATELY
It creates its own card and comments on it twice. It never touches a card
anyone else owns: on the version this defends against, a write to a card with
history DESTROYS rows, so probing a real card would risk committing the very
harm being measured. A freshly created card has no hidden rows by
construction, which is what makes writing to it safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The comment that discriminates. A broken build accepts the first and raises
#: on this one, because the merge only runs when prior rows exist.
_SECOND_COMMENT = "probe: second comment — this is the one that discriminates"

_FIRST_COMMENT = "probe: first comment — passes even on a broken build"

#: Verdicts. Three-valued on purpose: `unknown` is a real answer, not a
#: failure to produce one, and never folds into ok/broken.
OK = "ok"
BROKEN = "broken"
UNKNOWN = "unknown"


@dataclass
class CardWriteVerdict:
    """A fixed shape, so "I could not tell" cannot read as "yes".

    Every signal is its own named field; a caller never has to guess which key
    exists on this call.
    """

    verdict: str
    detail: str
    #: Which step reached its conclusion — the discriminating one is
    #: ``second_comment``. A verdict from any earlier step is ``unknown``,
    #: because the discriminating write never ran.
    step: str
    card_id: str | None = None
    hint: str = ""
    #: Populated only when a write raised; the exception's repr, not its str,
    #: because `KeyError: 0` loses its type when stringified.
    error: str = ""
    cleaned_up: bool = False
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.verdict not in (OK, BROKEN, UNKNOWN):
            raise ValueError(
                f"verdict must be one of {OK!r}/{BROKEN!r}/{UNKNOWN!r}, "
                f"got {self.verdict!r} — a malformed verdict must fail where "
                "it is built, not three layers downstream"
            )
        if self.verdict is BROKEN and not self.hint:
            raise ValueError("a BROKEN verdict must carry an actionable hint")

    @property
    def is_conclusive(self) -> bool:
        """True only when the discriminating write actually ran.

        Deliberately NOT ``verdict != UNKNOWN``: the point is that a
        conclusion requires the SECOND comment to have been attempted.
        """
        return self.step == "second_comment"


def build_probe_card_id(stamp: str) -> str:
    """Name the scratch card so a human finds it and knows it is disposable.

    ``stamp`` is passed in rather than read from the clock: a caller that
    cannot pass a stamp cannot make this reproducible, and a probe that
    invents its own id is one nobody can correlate with a deploy.
    """
    if not stamp or not stamp.strip():
        raise ValueError("stamp must be a non-empty string")
    return f"zz-probe-card-write-{stamp.strip()}"


def classify_write_failure(exc: BaseException) -> CardWriteVerdict:
    """Turn an exception from the second write into a verdict.

    A ``KeyError`` whose key is the INTEGER 0 is the signature of the
    positional-indexing defect, and it is worth naming explicitly rather than
    lumping it in with "something raised" — the hint that names the real cause
    is what turns an alert into a fix.
    """
    if isinstance(exc, KeyError) and exc.args and exc.args[0] == 0:
        return CardWriteVerdict(
            verdict=BROKEN,
            step="second_comment",
            detail=(
                "writing a second comment raised KeyError(0) — the card-store "
                "write path indexes rows positionally on a backend whose rows "
                "are dicts"
            ),
            error=repr(exc),
            hint=(
                "This image's scitex-cards cannot write to any card that "
                "already has comments, so agents cannot record or close their "
                "own work. Known in 0.49.0 (_mirror_rows._merge_unseen_"
                "comment_rows uses row[0] where psycopg's dict_row requires "
                'row["author"]). Install >=0.49.1 and re-run this probe.'
            ),
        )
    return CardWriteVerdict(
        verdict=BROKEN,
        step="second_comment",
        detail=f"writing a second comment raised {type(exc).__name__}",
        error=repr(exc),
        hint=(
            "The card-store write path failed on a card that already has "
            "comments. Read the error above, then re-run this probe against "
            "the same store before trusting this image to record work."
        ),
    )
