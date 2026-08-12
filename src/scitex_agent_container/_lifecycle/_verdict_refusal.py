"""The REFUSAL signal — CAN this agent act? (not: does its process exist)

THE INCIDENT (card ``sac-liveness-must-distinguish-quota-dead-from-idle``)
    2026-08-10, scitex-compute-04. ``scitex-cards`` hit the Claude weekly limit.
    Between ~15:45 and ~16:17 UTC its pane answered "You've hit your weekly
    limit · resets 11pm (UTC)" to every scheduled task AND to each of the
    operator's own messages. The process lived, the tmux session lived, the
    heartbeat ticked — so ``sac agents health`` and ``sac agents list`` reported
    HEALTHY for an agent that could not execute a single turn.

    The operator diagnosed it by reading the pane himself: 「scitex-cards が動
    いてねえんだ、たぶん scitex-compute-04 にいるのだろうけれど止まっているん
    じゃねえか、って、もう嫌なんだこういうの」 / 「おれはつかれた」. He should
    never have to read a pane to learn an agent is dead. That is the bar.

    It recurred in a second form on 2026-08-11 (~22:30Z): eight subagents died
    at once with "Login expired · Please run /login" while
    ``sac.accounts-refresh.timer`` had fired 23 minutes earlier and was
    scheduled normally, and ``account_show`` reported the account fine at
    5h=32% / 7d=44%. So "the refresher is alive" is not evidence that
    credentials are USABLE, and account quota being fine is not evidence that a
    session can ACT. Same shape, different cause: **a liveness signal that
    cannot observe the failure mode that actually stops work.**

WHY EVERY EXISTING SENSOR MISSED BOTH
    Each one answers "IS IT PRESENT?" — a pid, a session, a registry row, a
    bound port, a scheduled timer. None answers "CAN IT ACT?". A quota-dead
    agent scores perfectly on all of them, which is why it read HEALTHY for 32
    minutes while answering nothing.

    Even the ``screen`` instrument (:mod:`._verdict_screen`) — the one sensor
    that reads rendered CONTENT rather than presence — could not see it. It was
    built for the auth-death class and matches only auth banners; its matcher
    (``.._runners._tmux.auth_status``) deliberately EXCLUDES 429 with the
    comment "a restart does not fix a rate wall". That exclusion is correct for
    a RESTARTER and exactly wrong for a HEALTH REPORT: the one failure a restart
    cannot fix became the one failure nothing reported. **Not being actionable
    is not a reason to be invisible** — it is a reason to say so and name the
    remedy that does work.

    The field that was supposed to cover it, ``_heartbeat_fields._detect_capped``,
    promises in its own docstring "Always returns a bool — False (not absent)
    when there is no evidence", and scans ``<state_dir>/session.jsonl`` — a path
    that is empty for every TUI agent. Measured on the host 2026-08-12: **all 17
    agents with a heartbeat reported ``capped: false`` on
    ``session_jsonl_bytes: 0``** — a fleet-wide "not capped" asserted from zero
    bytes, which is the constitution's most common bug (§2, "Collapsing unknown
    into either pole") written into a field every dashboard trusts. This module
    does not touch that field; it publishes the honest signal beside it and
    health reads THIS.

WHAT IT READS — THE ARTEFACT THE FAILURE ITSELF WRITES
    A refused turn is recorded in the agent's own transcript as
    ``isApiErrorMessage: true`` with ``model: "<synthetic>"`` and zero token
    usage: no turn ran. Written BY the failure, BY the agent, at the moment it
    could not act — the fault's own receipt rather than a proxy for it, which is
    why it survives both incidents where every proxy passed. Reading and
    classification live in :mod:`._verdict_refusal_read`; the real records from
    both incidents are checked in under
    ``tests/scitex_agent_container/fixtures/refusals/``.

WHAT IT MAY CONCLUDE
    :data:`.._verdict_instruments.WEDGED` (present but unable to act) on a fresh
    refusal, and :data:`.._verdict_instruments.UNKNOWN` on everything else —
    including a CLEAN read, because a turn that worked a minute ago is not
    evidence the agent is working now. Never ALIVE, never DEAD: an agent
    refusing turns is emphatically present, and a DEAD would arm a destruction
    against a living process whose only fault is an exhausted quota — which a
    restart does not fix, and which would cost it its context.

WHY IT OUTRANKS A DELIVERY-ALIVE IN THE FOLD
    :func:`._verdict.decide` ranks a ``delivery`` ALIVE above everything,
    including WEDGED, on the reasoning that a broker-reachable agent is
    "demonstrably working". That premise held for the founding auth-death
    incident, whose agent was NOT reachable. It does not hold here, and the
    reported incident is the counter-example: the operator's messages REACHED
    ``scitex-cards`` — that is how they got refused, one refusal per message.

    Delivery observes that a message can ARRIVE. It does not observe that a turn
    can EXECUTE. When the agent's own record says the turn did not run, that is
    the more direct observation of the property in question, from the agent
    itself. So a refusal WEDGE is the one signal ranked above a delivery ALIVE —
    and only this instrument's, because only this one reads the agent's
    testimony about its own turns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ._verdict_instruments import (
    INSTRUMENT_TURN_REFUSAL,
    SOURCE_TRANSCRIPT,
    UNKNOWN,
    WEDGED,
    Signal,
)
from ._verdict_refusal_read import (
    DEFAULT_STALE_AFTER_S,
    STATE_REFUSED,
    RefusalRead,
    find_transcript,
    last_turn_refusal,
)

__all__ = ["refusal_signal"]


def refusal_signal(
    name: str,
    *,
    config: Any | None = None,
    read_fn: "Callable[..., RefusalRead] | None" = None,
    find_fn: "Callable[[Any], tuple[Path | None, tuple[str, ...]]] | None" = None,
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
) -> Signal:
    """Can ``name`` execute a turn? A :class:`Signal` for the liveness fold.

    Emits :data:`.._verdict_instruments.WEDGED` on a FRESH refusal, carrying the
    cause, the provider's own words and the remedy; :data:`UNKNOWN` for every
    other outcome, each naming what was read and why no verdict follows from it.

    ``read_fn`` / ``find_fn`` are injection seams taking REAL callables (the
    suite drives real transcript files through them), defaulting to
    :func:`._verdict_refusal_read.last_turn_refusal` and
    :func:`._verdict_refusal_read.find_transcript`. Never raises — a health
    command must not be taken down by an unreadable file.
    """
    if config is None:
        return Signal(
            SOURCE_TRANSCRIPT,
            UNKNOWN,
            f"no config for {name!r}, so its transcript could not be located — "
            f"nothing was read, which is not evidence that its turns are running",
            INSTRUMENT_TURN_REFUSAL,
        )

    transcript, homes = (find_fn or find_transcript)(config)
    if transcript is None:
        return Signal(
            SOURCE_TRANSCRIPT,
            UNKNOWN,
            f"no transcript found for {name!r} under {list(homes)} — those are "
            f"the homes its SPEC promises, not a fact about where this "
            f"incarnation writes, so it may be running from a home this host "
            f"cannot see. Its ability to act was NOT observed",
            INSTRUMENT_TURN_REFUSAL,
        )

    read = (read_fn or last_turn_refusal)(
        transcript, now=now, stale_after_s=stale_after_s
    )
    verdict = WEDGED if read.state == STATE_REFUSED else UNKNOWN
    return Signal(SOURCE_TRANSCRIPT, verdict, read.detail, INSTRUMENT_TURN_REFUSAL)
