"""Is this pane parked behind a MODEL CAP — a wall a MODEL SWITCH can end NOW?

PURE — no tmux, no clock of its own, no I/O. Every input is passed in, so the
whole table below is exercised against the two banners actually measured.

WHY A SECOND MATCHER NEXT TO ``_banner`` INSTEAD OF A WIDER ``LIMIT_RE``
-----------------------------------------------------------------------
:mod:`._banner` answers *is there a rate wall, and WHEN does it lift* — a
question whose only correct remedy is to WAIT. This module answers a different
question about the same screen: *is this wall one the operator can end right
now by switching models*. The two answers overlap by construction on the
session-limit rendering, and that overlap is deliberate rather than a bug: one
pane line is BOTH a pause with a published end AND, on a Fable-family agent, a
pause that a ``/model`` switch ends in seconds. Which remedy applies is a
question about the AGENT (what model is it on?), not about the pixels, so it
belongs in the rule above — :mod:`._switch_rule` — and not in either matcher.

Keeping ``LIMIT_RE`` untouched is the other half of that reasoning. Widening it
to also mean "switchable" would make one regex carry two remedies with opposite
correct actions, and the first ambiguous banner would then pick the wrong one
silently.

THE SPECIMENS THIS IS BUILT ON — measured 2026-09-06, not invented
------------------------------------------------------------------
The operator sent two messages to a Fable-family agent tonight. BOTH were
answered by the harness with::

    You've reached your Fable limit. Run /usage-credits to continue or switch
    models with /model.

and three workflow subagents died in the same window with::

    You've hit your session limit · resets 2am (UTC)

So the trigger has (at least) two shapes and the verbs differ — ``reached`` in
the first, ``hit`` in the second. A matcher fitted to only one of them would
have reported a healthy fleet through the outage that produced the other, which
is exactly the failure ``_banner`` was written to avoid, so both verbs are
matched here and both strings are kept verbatim in
:data:`CAPPED_SPECIMEN_PANES` for the tests to assert against.

Note what the FIRST specimen does not have: a reset clause. It publishes no end
time at all, so :mod:`._banner`'s waiting machinery has nothing to wait for and
:mod:`._rule` correctly reports ``NOT-LIMITED`` for it (``LIMIT_RE`` needs
``hit``, not ``reached``). That silence is the whole reason this module exists:
a cap with no published end is not a pause to wait out, it is a wall to walk
around — and the banner itself says how, by naming ``/model``.

THE OTHER MATCHER FOR THE SAME SENTENCE, and why it is not reused
------------------------------------------------------------------
``_account/rate_limit_signals.py`` (PR #1288, merged 2026-09-06) added
``reached your \\w+ limit`` and ``/usage-credits`` to
``DEFAULT_TEXTUAL_PATTERNS`` from the same banner. That is not a duplicate of
this one and neither should collapse into the other: it scans ERROR TEXT to
answer *should this ACCOUNT be rotated or paused*, and it deliberately fires
on the whole family of cap phrasings (``weekly limit``, ``usage limit``,
``quota exceeded``). This one scans a tmux PANE to answer *should this
AGENT's model be changed*, and the widening that is right there would be
wrong here: firing a model switch on every weekly-window phrasing would move
agents off models that were never capped. Two consumers, two populations, one
sentence they happen to share.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It never claims a switch SUCCEEDED from the absence of a banner alone, and it
never matches text sac itself typed into the pane. See :func:`verify_switch`,
where both refusals are implemented and the self-match trap is spelled out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

__all__ = [
    "CAPPED_SPECIMEN_PANES",
    "MODEL_CAP_RE",
    "ModelCapObservation",
    "SwitchEvidence",
    "observe_model_cap",
    "verify_switch",
]

#: The cap banner. ``reached``/``hit`` both matched — the two measured
#: renderings use different verbs, and a matcher fitted to one of them reports
#: a healthy fleet through an outage of the other. Anchored on the provider's
#: own first-person sentence, exactly as ``_banner.LIMIT_RE`` is, so an agent
#: writing PROSE about the incident ("switcher fired on the Fable limit") does
#: not match. Both apostrophes — ASCII and U+2019 — because the capture shows
#: whichever the renderer chose, and a detector that silently stopped matching
#: on a typographic quote is a detector that reports good news during an
#: outage.
MODEL_CAP_RE = re.compile(
    r"you['’]ve\s+(?:reached|hit)\s+your\s+(?P<subject>[a-z0-9-]+)\s+limit",
    re.IGNORECASE,
)

#: The remedy the provider itself offers. Its presence is what makes the first
#: specimen self-describing: the banner names ``/model`` as the way out, which
#: is precisely the mutation :mod:`._switch` performs. Absence is NOT evidence
#: against switching (the session-limit rendering never prints it), so this is
#: carried as an observation and never used as a gate.
_REMEDY_RE = re.compile(r"switch\s+models?\s+with\s+/model", re.IGNORECASE)

#: Verbatim captures, kept in the module they justify. The tests assert against
#: these, so a rendering change that breaks the matcher breaks a test rather
#: than silently returning "not capped".
CAPPED_SPECIMEN_PANES: tuple[str, ...] = (
    "You've reached your Fable limit. Run /usage-credits to continue or "
    "switch models with /model.",
    "You've hit your session limit · resets 2am (UTC)",
    "⎿ You’ve reached your Fable limit. Run /usage-credits to "
    "continue or switch models with /model.",
)


@dataclass(frozen=True)
class ModelCapObservation:
    """One pane, read once, for a SWITCHABLE cap. THREE states, never two.

    ``readable=False`` is not ``capped=False``. A pane we could not capture
    told us nothing about that agent, and reporting "not capped" there would
    be an instrument announcing good news about something it never saw — the
    same distinction :class:`._banner.LimitObservation` draws, kept identical
    on purpose so the two matchers cannot disagree about what blindness means.

    ``subject`` is the word the banner put before "limit" — ``fable`` in the
    first measured rendering, ``session`` in the second. It is reported RAW
    and uninterpreted: whether that word names a model family is knowledge
    about models, which lives in :mod:`._switch_rule`, not in a pane parser.

    ``line_index`` is the pane line the banner was found on, counted from the
    TOP of the capture, and exists for the freeze comparison in the rule — a
    banner that MOVED between two captures means the pane is still producing
    output, which is an agent working, never one parked behind a cap.
    """

    readable: bool
    capped: bool = False
    subject: str = ""
    remedy_offered: bool = False
    reset_at: datetime | None = None
    reset_text: str = ""
    line_index: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class SwitchEvidence:
    """Did the model switch take? ``True`` / ``False`` / ``None``.

    ``None`` is a first-class answer and the one this type exists for: a
    switch we could not verify must never be counted as a switch we made.
    The pass reports it as ``SWITCH-UNVERIFIED`` and exits 2, so an
    ambiguity costs a human a look rather than costing the operator a silent
    agent he believes was recovered.
    """

    switched: bool | None
    detail: str


def observe_model_cap(
    pane: str | None, *, now: datetime, default_tz: timezone
) -> ModelCapObservation:
    """Read ONE captured pane for a switchable model cap. Pure.

    Scans from the BOTTOM up and reports the LAST banner, because a pane is a
    scrolling log: an older cap that has already been dealt with sits above a
    newer one, and the newest line is the only one describing the agent's
    current state. Same direction as :func:`._banner.observe_pane`, for the
    same reason.

    Left TUI decoration is stripped with the SAME stripper both sibling
    matchers use, so the NBSP that Claude's Ink TUI renders after ``⎿`` is
    handled in one place. That NBSP is not a detail: leaving it out of the
    strip set once already made a real banner undetectable.

    A reset clause is parsed when the rendering carries one (the session-limit
    shape does; the Fable shape does not) and is reported as ``None`` when it
    is absent or unreadable. It is INFORMATION here, never a gate: a cap with
    no published end is the case this whole path exists for.
    """
    if pane is None:
        return ModelCapObservation(
            readable=False,
            detail="pane could not be captured — NO evidence, which is not "
            "evidence of a working agent",
        )

    from .._runners._tmux.auth_status import _strip_markers
    from ._banner import parse_reset_at

    lines = pane.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = _strip_markers(lines[index])
        found = MODEL_CAP_RE.search(stripped)
        if found is None:
            continue
        reset_at, raw = parse_reset_at(stripped, now=now, default_tz=default_tz)
        subject = found.group("subject").lower()
        remedy = _REMEDY_RE.search(stripped) is not None
        when = (
            f"lifting at {reset_at.isoformat()} (read from {raw!r})"
            if reset_at is not None
            else "with NO published end time, so there is nothing to wait for"
        )
        return ModelCapObservation(
            readable=True,
            capped=True,
            subject=subject,
            remedy_offered=remedy,
            reset_at=reset_at,
            reset_text=raw,
            line_index=index,
            detail=(
                f"a {subject!r} cap banner is rendered at pane line {index}, "
                f"{when}"
                + (
                    " — and the banner itself names /model as the way out"
                    if remedy
                    else ""
                )
            ),
        )
    return ModelCapObservation(
        readable=True,
        capped=False,
        detail="no model-cap banner anywhere in the captured pane",
    )


def verify_switch(
    pane: str | None,
    *,
    target_model: str,
    sent_texts: tuple[str, ...] = (),
    kick_submitted: bool | None = None,
    now: datetime,
    default_tz: timezone = timezone.utc,
) -> SwitchEvidence:
    """Did the three-step switch actually take? Pure, and THREE-VALUED.

    Called with the pane captured AFTER the kick, so it answers about the
    screen the operator would see. Never assumes success from a send that
    returned 0 — a ``tmux send-keys`` exit status says a keystroke was
    accepted by tmux, not that a model changed.

    THE SELF-MATCH TRAP, and why ``sent_texts`` is not optional in practice
    ---------------------------------------------------------------------
    sac TYPES the target model into this very pane (``/model opus[1m]``), and
    the TUI echoes what was typed. A verifier that simply searched the pane
    for "opus" would therefore find its own keystrokes and certify every
    attempt, successful or not — the pattern matching its own process. So the
    count of the flattened target in the pane is compared against the count
    sac itself contributed: only occurrences BEYOND our own are evidence.

    The ladder, strongest rung first:

    * pane unreadable                 -> ``None``. We could not look.
    * the cap banner is STILL rendered -> ``False``. Whatever we typed, the
      wall is still on the screen.
    * the target appears MORE times than we typed it -> ``True``. Something
      other than our own echo is naming the target model.
    * the cap is gone AND the kick was PROVEN to leave the compose box ->
      ``True``. A capped agent cannot take a turn, so a submitted prompt on a
      pane with no cap banner is a working agent.
    * anything else -> ``None``. The banner is gone and nothing proved the
      switch; that is an ambiguity, and it is reported as one.

    ``flatten_pane`` (the delivery package's) is reused rather than copied:
    it strips every non-alphanumeric character, which defeats the Ink TUI's
    soft wraps and box borders at once. A model id survives it as
    ``opus1m``.
    """
    if pane is None:
        return SwitchEvidence(
            None,
            "the pane could not be captured after the switch — this is 'we "
            "could not look', which must never be spelled the same way as "
            "'it worked'",
        )

    from .._delivery._token import flatten_pane

    still_capped = observe_model_cap(pane, now=now, default_tz=default_tz)
    if still_capped.capped:
        return SwitchEvidence(
            False,
            f"the cap banner is STILL rendered after all three steps "
            f"({still_capped.detail}) — the switch did not take, and this "
            f"agent is still silent",
        )

    # ``ours`` counts only the sent texts that are ACTUALLY on this screen.
    # Subtracting a payload that has already scrolled away would over-correct
    # and hide a real switch; not subtracting one that is still rendered would
    # certify a switch out of sac's own keystrokes. Presence decides, which is
    # the only version of this arithmetic that is right in both directions.
    flat = flatten_pane(pane)
    needle = flatten_pane(target_model)
    ours = 0
    if needle:
        for text in sent_texts:
            flat_text = flatten_pane(text)
            if flat_text and flat_text in flat:
                ours += flat_text.count(needle)
    seen = flat.count(needle) if needle else 0
    if needle and seen > ours:
        return SwitchEvidence(
            True,
            f"the cap banner is gone and {target_model!r} is named {seen} "
            f"time(s) on the pane against the {ours} sac typed there, so "
            f"something other than our own echo is reporting the target model",
        )
    if kick_submitted is True:
        return SwitchEvidence(
            True,
            "the cap banner is gone and the kick was PROVEN to leave the "
            "compose box — a capped agent cannot accept a turn, so this is a "
            "working agent",
        )
    return SwitchEvidence(
        None,
        f"the cap banner is gone, but nothing PROVED the switch: "
        f"{target_model!r} is named {seen} time(s) against the {ours} sac "
        f"typed, and the kick was not provably submitted "
        f"(kick_submitted={kick_submitted!r}). Reporting an ambiguity rather "
        f"than claiming a recovery we cannot show",
    )
