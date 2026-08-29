"""Make a mute agent LOUD — over a rail that is not the broken one.

START AND SHOUT, NOT REFUSE TO START
------------------------------------
The obvious alternative was to fail the start when an agent declares the
telegrammer channel and no slot resolves. It is rejected, for three measured
reasons:

1. **It would brick the fleet.** Measured on compute-04: 89 registered specs
   declare ``server:claude-code-telegrammer`` and the pool holds 18 slots.
   The declaration is inherited from ``_template_generalist`` /
   ``_template_python_developer`` / ``_template_researcher``, so it measures
   SCAFFOLDING, not intent. Refusing on it would refuse most of the fleet.

2. **Telegram is a comms rail, not a boot dependency.** That is already this
   subsystem's stated contract, and it is already an operator ruling: the
   missing-token log was demoted ERROR → WARNING precisely because a
   brand-new agent has no bot BY DEFINITION and was being made to look
   stillborn.

3. **Refusing makes the silence worse, not better.** A stranded agent cannot
   do the non-Telegram work it was started for, and it removes the one
   process that could have reported the problem. The failure being closed
   here is "nobody was told"; killing the teller does not fix it.

WHERE THE ALARM LANDS, AND WHY IT CANNOT BE A LOG LINE
------------------------------------------------------
The shout cannot go over Telegram — Telegram is what is broken. And it cannot
be *only* a log line: that was tried and measured. On 2026-08-10 four agents on
a new host went mute and deaf behind one INFO line each; the operator, getting
no answers, concluded they were ignoring him. A log nobody reads is silence
with extra steps.

So the alarm takes sac's two EXISTING rails, both already used by five sibling
alarm modules, and neither of which touches the broken agent's Telegram:

* **The record** — :func:`.._events.emit_subject_verdicts` under subsystem
  :data:`SUBSYSTEM`. Durable, sac-owned, three-valued at the source
  (``DEGRADED`` / ``UNKNOWN`` / ``HEALTHY``), transition-tracked so an ongoing
  fault is re-recorded but a well fleet writes nothing. This is what makes the
  rail trustworthy: it does not depend on a lead being configured, on the
  network, or on any other software being installed.

* **The push** — :func:`.._state.lead_inbox.push_to_lead` with
  ``kind="blocker"`` (ADR-0013), the same agent→lead rail agents already use
  for "creds expired". **This is the load-bearing choice.** It reaches the
  operator through the LEAD's Telegram session, which is a DIFFERENT agent
  with a DIFFERENT bot. A mute agent can still shout, because it shouts with
  somebody else's voice.

The record comes first and the push is best-effort on top: they fail
independently, and only the record failing leaves sac with no account of the
outage. Deduped to one push per agent per outage — a `blocker` that re-pages on
every start would train the operator to ignore it, which is the failure mode
this module exists to prevent, rebuilt as a feature.

WHY A CARD IS NOT THE ANSWER HERE
---------------------------------
A board card was the other candidate and is a good instinct — persistent,
actionable, self-extinguishing. It is not used because sac does not depend on
scitex-cards, and the fix for "the operator was not told" must not itself be a
new cross-package dependency that can be absent at exactly the moment it is
needed. The lead rail is already installed, already ACL-gated, already
durable in the lead listen's ``channel_events`` store, and already the thing
the lead relays to the operator. No new delivery rail is invented.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from .._events import (
    SubjectState,
    SubjectVerdict,
    degraded_state_path,
    emit_subject_verdicts,
)
from ._cct_rail_verdict import (
    RAIL_DOWN,
    RAIL_NOT_REQUESTED,
    RAIL_UNKNOWN,
    RAIL_UP,
    CctRailVerdict,
    assess_cct_rail,
)

#: The pass this module speaks for — the axis a reader filters the log on.
SUBSYSTEM = "cct-rail"

_STATE_FOR = {
    RAIL_DOWN: SubjectState.DEGRADED,
    RAIL_UNKNOWN: SubjectState.UNKNOWN,
    RAIL_UP: SubjectState.HEALTHY,
}


def _seen_up_path(path: Path | None) -> Path:
    """Where "this agent's rail has worked here before" is remembered.

    Beside the event log, like the degraded set, so redirecting the log in a
    test carries its state with it.
    """
    from .._events import degraded_state_path

    base = degraded_state_path(SUBSYSTEM, path=path)
    return base.with_name(f"sac-events-{SUBSYSTEM}-seen-up.json")


def _read_set(target: Path) -> set[str]:
    # stx-allow: fallback (reason: a missing or corrupt memory file must degrade to "remember nothing"; the cost is at most one missed regression page, never a crashed start)
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return set()
    return {str(x) for x in loaded} if isinstance(loaded, list) else set()


def _remember_up(agent: str, path: Path | None) -> None:
    """Record that this agent's rail HAS worked here. Never raises."""
    target = _seen_up_path(path)
    known = _read_set(target)
    if agent in known:
        return
    known.add(agent)
    # stx-allow: fallback (reason: this file only sharpens a future page; failing to persist it must not disturb a start that already succeeded)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(sorted(known), indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass


def page_is_warranted(verdict: CctRailVerdict, *, path: Path | None = None) -> bool:
    """Does this verdict deserve to interrupt a human, or only to be recorded?

    EVERY alarming verdict is RECORDED, and the fleet sweep
    (``sac agents cct-audit``) lists every one of them. This gate decides only
    whether a ``blocker`` is PUSHED at the lead.

    IT HAS TO EXIST, and the number is the argument. Measured on compute-04,
    2026-08-12, with the real pool: **81 specs declare the telegrammer channel,
    15 resolve a token, 66 do not.** The 66 are overwhelmingly library and tool
    agents (``scitex-io``, ``scitex-math``, …) plus the three spec TEMPLATES,
    which inherit the channel request as SCAFFOLDING and were never meant to
    have a bot. Paging on all 66 would put the operator back where the
    2026-08-10 prune found him — an alert channel he has learned to ignore —
    only louder. That is not a fix; it is the same failure re-shipped as a
    feature. The same reasoning already produced an operator ruling: the prune's
    ERROR is keyed on the DECLARED slot and deliberately not on the channel
    request.

    So a DOWN pages only on EVIDENCE that somebody meant this agent to have a
    bot. Three kinds, each independently sufficient:

    * **A declared slot** — ``spec.apptainer.env: CCT_BOT_TOKEN_SLOT``. Somebody
      typed this mapping on purpose and it does not work. (The 2026-08-10
      ruling, unchanged.)
    * **A near miss** — the pool holds a slot that shares a word with this
      agent. Somebody provisioned a bot that plausibly belongs here and the
      wiring does not reach it. Measured: this fires for exactly 4 of the 66
      (``neurovista``, ``neurovista-paper-writer``, ``scitex-clew``,
      ``spartan-dev``), and all four are genuine defects — including two the
      sweep found that nobody had reported.
    * **A rail that USED to work here** — the regression shape, and the reason
      the outage was noticed by silence rather than by a signal. An agent that
      resolved a token on this host and now does not has LOST something.

    UNKNOWN always pages, regardless of evidence: it is rare, it is usually
    systemic (one missing ``SAC_SECRETS_ENVRC`` blinds the whole fleet at once),
    and "I could not tell" that nobody reads is exactly the collapse into a
    false all-clear this work exists to prevent.
    """
    if verdict.state == RAIL_UNKNOWN:
        return True
    if verdict.state != RAIL_DOWN:
        return False
    if verdict.declared_slot or verdict.near_misses:
        return True
    return verdict.agent in _read_set(_seen_up_path(path))


def _already_reported(subject: str, *, path: Path | None) -> bool:
    """Was ``subject`` already in the remembered degraded set BEFORE this pass?

    Read directly from :func:`.._events.degraded_state_path`, whose
    documented on-disk shape is a JSON list of subject names. The set is an
    OPTIMISATION OF THE RECORD, never an input to a decision — losing it costs
    one duplicate page, never a missed one, which is the right direction for a
    dedupe on an alarm about silence.
    """
    target = degraded_state_path(SUBSYSTEM, path=path)
    # stx-allow: fallback (reason: an unreadable dedupe file must degrade to "not yet reported" — the cost is one duplicate page, whereas the opposite default would silently swallow the very alarm this module exists to deliver)
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return False
    return isinstance(loaded, list) and subject in {str(x) for x in loaded}


def _summary(verdict: CctRailVerdict) -> str:
    """One line, and it must say MUTE — that is the operator-visible symptom."""
    if verdict.state == RAIL_DOWN:
        return (
            f"[cct] agent {verdict.agent!r} started MUTE and DEAF on Telegram — "
            f"it declares the telegrammer channel but NO bot-token slot resolves, "
            f"so sac removed the MCP server. You will hear nothing from it."
        )
    return (
        f"[cct] agent {verdict.agent!r} may be MUTE on Telegram — sac could NOT "
        f"determine whether a bot-token slot resolves for it. This is an unread "
        f"instrument, not an all-clear."
    )


def _detail(verdict: CctRailVerdict) -> str:
    lines = [
        f"agent: {verdict.agent}",
        f"state: {verdict.state}",
        f"declared_slot: {verdict.declared_slot or '(none)'}",
        f"slots_tried: {', '.join(verdict.candidates) or '(none)'}",
        f"pool_source: {verdict.pool_source or '(not read)'}",
        f"pool_read_conclusive: {verdict.pool_trusted}",
        "",
        verdict.detail,
        "",
        verdict.remedy(),
        "",
        "No token value was read, logged, or transmitted — slot NAMES only.",
    ]
    return "\n".join(lines)


def _push_to_lead(summary: str, detail: str) -> None:
    """The ADR-0013 agent→lead ``blocker`` push. Raises on any delivery failure.

    ``from_agent`` is ``$SAC_NAME`` when set, else the lead's own name — a
    self-send, which the ``message:send`` ACL always admits, so a host-side
    start (systemd, cron, ssh) can never have its alarm dropped by a group
    check. That matters here more than usual: the host-side start path is
    exactly the one whose environment was missing in the 2026-08-12 outage.
    """
    from .._state.lead_inbox import push_to_lead, resolve_lead

    lead = resolve_lead()
    push_to_lead(
        kind="blocker",
        summary=summary,
        detail=detail,
        from_agent=os.environ.get("SAC_NAME", "").strip() or lead.name,
        lead=lead,
    )


def alarm_cct_rail(
    verdict: CctRailVerdict,
    *,
    path: Path | None = None,
    push: Callable[[str, str], None] | None = None,
    now: float | None = None,
    err_stream: Any = None,
) -> str:
    """Record ``verdict`` and, when it is NEWLY alarming, page the operator.

    Returns what this call did: ``"paged"``, ``"recorded"`` (alarming, and
    recorded, but not pushed — already reported for this outage, not warranted
    by :func:`page_is_warranted`, or the push failed), ``"clear"`` (the rail is
    up), or ``"skipped"`` (the spec never asked for the rail).

    EVERY alarming verdict is RECORDED. Only the PUSH is gated — see
    :func:`page_is_warranted` for the measurement behind that split.

    Never raises. An alarm that can crash a start is an outage generator, and
    this one is attached to every start in the fleet.

    Parameters
    ----------
    path, push, now, err_stream
        Test seams: an explicit event-log path (its sibling state file follows
        it automatically), a replacement delivery callable called as
        ``push(summary, detail)``, a fixed clock, and a replacement stderr.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    if verdict.state == RAIL_NOT_REQUESTED:
        return "skipped"

    was_reported = _already_reported(verdict.agent, path=path)
    emit_subject_verdicts(
        SUBSYSTEM,
        [
            SubjectVerdict(
                subject=verdict.agent,
                state=_STATE_FOR[verdict.state],
                verdict=verdict.state,
                detail=_detail(verdict),
                subject_kind="agent",
                extra={
                    "declared_slot": verdict.declared_slot,
                    "slots_tried": list(verdict.candidates),
                    "near_miss_slots": list(verdict.near_misses),
                    "pool_read_conclusive": verdict.pool_trusted,
                },
            )
        ],
        path=path,
        now=now,
        err_stream=stream,
    )

    if not verdict.is_alarming:
        # A working rail is remembered, so that LOSING it later is recognisable
        # as a regression rather than as one more agent that never had a bot.
        _remember_up(verdict.agent, path)
        return "clear"
    if was_reported:
        return "recorded"
    if not page_is_warranted(verdict, path=path):
        return "recorded"

    send = push if push is not None else _push_to_lead
    # stx-allow: fallback (reason: the durable record above already succeeded, so a lead-push failure loses attention-now, not the fact; it is printed loudly and must never abort the agent start it is attached to)
    try:
        send(_summary(verdict), _detail(verdict))
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[cct-rail] {verdict.agent}: recorded in sac's event log, but the "
            f"lead blocker push FAILED — {exc}. NOBODY HAS BEEN PAGED; this "
            f"agent is {verdict.state} on Telegram and the only account of it "
            f"is the event log.",
            file=stream,
        )
        return "recorded"
    return "paged"


def check_cct_rail_at_start(config, *, dest: Path | None = None, **kwargs) -> str:
    """Assess ``config``'s Telegram rail and alarm on it. The start-time hook.

    Deliberately NOT wired into :func:`._to_home.deploy_to_home`, which is
    where the resolution itself happens: that function also runs under
    ``sac agents explain`` against a throwaway temp dir, and an alarm that
    pages the operator from a dry run is an alarm that gets muted.

    Never raises — see :func:`alarm_cct_rail`.
    """
    # stx-allow: fallback (reason: this is a diagnostic attached to every start in the fleet; a bug in the ASSESSMENT must degrade to a printed warning, never take down the start it was added to protect)
    try:
        return alarm_cct_rail(assess_cct_rail(config, dest=dest), **kwargs)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        # No caller-injected stream here (unlike alarm_cct_rail's err_stream
        # seam a few lines up), so this had no reporting contract to honour and
        # is routed through scitex-logging: it is the only account of a rail
        # assessment that silently did not happen.
        from .._logging import get_logger

        get_logger(__name__).warning(
            f"[cct-rail] could not assess the Telegram rail for "
            f"{getattr(config, 'name', '?')!r}: {exc}. The agent starts "
            f"normally; its rail state is UNOBSERVED."
        )
        return "skipped"


__all__ = [
    "SUBSYSTEM",
    "alarm_cct_rail",
    "check_cct_rail_at_start",
    "page_is_warranted",
]
