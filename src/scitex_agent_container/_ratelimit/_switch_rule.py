"""The PURE rule: may sac switch THIS agent off a capped model, and onto what?

No IO, no clock of its own — ``now`` is passed in — so every leg below is
driven directly by tests instead of inferred from a live fleet. Sibling of
:mod:`._rule`, sharing its :class:`._rule.Verdict` vocabulary on purpose: one
pass, one alphabet, one set of counts.

THE SHAPE THIS ADDS, and why waiting was the wrong remedy for it
----------------------------------------------------------------
:mod:`._rule` recovers an agent behind a wall THAT PUBLISHES ITS OWN END: it
waits for the reset the provider printed and then continues the agent. That is
right for a session/weekly rate wall and it is the only correct action there.

It is NOT right for a MODEL cap. Measured 2026-09-06: the operator sent two
messages to a Fable-family agent and both were answered by the harness with
``You've reached your Fable limit. Run /usage-credits to continue or switch
models with /model.`` — a wall with NO published end and an explicit remedy
printed on it. Waiting for a reset that was never announced means waiting
forever; three workflow subagents died in the same window behind
``You've hit your session limit · resets 2am (UTC)``, which does publish an
end, but an end HOURS away. In both cases the agent went silent to the
operator, and in both cases a model switch would have had it working in
seconds.

So this rule answers the question :mod:`._rule` cannot: not *when does this
lift* but *can we step around it right now*.

THE PRINCIPLES THIS TABLE ENCODES
---------------------------------
1. **A NAMEABLE family, capped on Fable.** The remedy is "get off the capped
   model", so it is meaningless on an agent that is not on it, and an agent
   capped on anything else keeps TODAY'S verdict — the pass falls back to
   :func:`._rule.decide`, unchanged. Two facts can put an agent in: its SPEC
   names the capped family, or the spec names some OTHER family sac can read
   while the BANNER names the capped one. That second leg is not generosity,
   it is the only leg that reaches the measured incident: a spec records what
   the agent was LAUNCHED on and lags a switch made in the TUI, and measured
   2026-09-06 not ONE of the fleet's 119 specs names ``fable`` at all, so a
   spec-only gate would be a guard that can never fire.
   A spec whose family sac CANNOT name is out, unconditionally, and that
   refusal is what makes the second leg safe: the banner is corroboration,
   never authority, because the provider's cap sentence lives VERBATIM in
   this repository (:data:`._modelcap.CAPPED_SPECIMEN_PANES`, two module
   docstrings, three test modules) and an agent that read one of those files
   and then went idle carries the trigger frozen on its own pane. Keeping the
   unnameable family out is what stops that from re-homing a local-model
   agent onto Anthropic Opus. This rule never widens the population it acts
   on; it only ever carves a switchable subset out of it.
2. **Idempotence is a rule, not a hope.** An agent already on the target is
   :attr:`._rule.Verdict.ALREADY_ON_TARGET` and is never touched. Sending
   ``/model opus[1m]`` to an agent already on ``opus[1m]`` would spend three
   keystrokes and a kick to achieve nothing, and the kick is a real turn on a
   real agent.
3. **Never twice for the same incarnation.** ``already_switched`` is the
   caller's memory of a switch it already performed and cannot yet see the
   result of, and it HOLDS. Combined with leg 4 this is the whole no-hot-loop
   argument: either the pane advanced (the agent took a turn — leg 4 stops
   us) or it did not (we already acted — leg 3 stops us).
4. **A moving pane is a working agent.** A cap banner at a different pane
   position across two captures means output is still being produced, so the
   agent is working, or quoting the incident. Never touched. Same freeze
   discipline as both siblings, for the same reason: a false positive here
   interrupts an agent that was fine — and this remedy's false positive is
   expensive, because it changes the model the agent runs on.
5. **"I could not look" is not "nothing is there"** — the rule sac's other
   enforcers already live by, kept identical here so all of them agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ._modelcap import ModelCapObservation
from ._rule import MANAGED_POLICIES, Decision, Verdict

__all__ = [
    "CAPPED_FAMILIES",
    "TARGET_MODEL",
    "decide_switch",
    "model_family",
]

#: What a capped Fable agent is switched ONTO. The operator's own words,
#: 2026-09-06: *"implement fable to opus automatic switcher when limit error
#: raised"*, and then the mechanism, step 1: ``/model opus[1m]``. The ``[1m]``
#: context suffix is deliberate and is carried verbatim into the keystroke —
#: dropping it would silently re-home the agent on the 200k context window,
#: which is a different agent from the one that was working.
TARGET_MODEL = "opus[1m]"

#: The model families this remedy applies to. ONE entry, on purpose: the
#: measured cap is Fable's, and a switcher that fires on families nobody has
#: seen capped would be acting on a rule nobody has evidence for. Widening
#: this is a one-line change WITH a specimen to justify it.
CAPPED_FAMILIES = ("fable",)

#: Every model family sac can name. Matched as a substring of the model id
#: because the id arrives in several shapes — the bare alias (``fable``), the
#: versioned form (``claude-fable-5``), and either with a context suffix
#: (``opus[1m]``). A provider model id that names none of them yields ``""``,
#: which is an honest "we cannot tell what family this is" and never fires.
_FAMILY_RE = re.compile(r"(opus|sonnet|haiku|fable)", re.IGNORECASE)


def model_family(model: str) -> str:
    """The family a model id belongs to, lowercased — ``""`` when unknown.

    ``claude-fable-5`` / ``fable`` -> ``fable``; ``claude-opus-4-8[1m]`` /
    ``opus[1m]`` -> ``opus``; a Qwen or other provider id -> ``""``.

    ``""`` is a real answer and the rule treats it as such: an agent whose
    family we cannot name is neither on the capped family nor on the target,
    so it is left to today's verdict. Guessing would be this enforcer
    changing the model of an agent it does not understand.
    """
    found = _FAMILY_RE.search(model or "")
    return found.group(1).lower() if found else ""


@dataclass(frozen=True)
class SwitchDecision:
    """A verdict plus WHY, plus the model we would move the agent TO.

    ``target`` is populated only on :attr:`._rule.Verdict.SWITCH_MODEL`; every
    other verdict leaves it empty, because naming a target we are not going to
    send would read, in a log, exactly like one we did.
    """

    verdict: Verdict
    reason: str
    detail: str
    target: str = ""

    @property
    def fires(self) -> bool:
        """Does this decision authorise the mutation?"""
        return self.verdict is Verdict.SWITCH_MODEL

    def as_decision(self) -> Decision:
        """The sibling shape, for the pass's shared reporting path."""
        return Decision(self.verdict, self.reason, self.detail)


def decide_switch(
    *,
    name: str,
    policy: str,
    session_present: bool | None,
    spec_model: str,
    first: ModelCapObservation,
    second: ModelCapObservation,
    now: datetime,
    target: str = TARGET_MODEL,
    already_switched: bool = False,
) -> SwitchDecision:
    """Decide ONE agent's model fate from facts alone. Pure — no IO, no clock.

    Parameters
    ----------
    policy
        ``spec.restart.policy``. Same opt-in as every other sac enforcer.
    session_present
        Whether a live tmux session for ``name`` was found. ``None`` means the
        enumeration itself failed, which is not the same as "no session".
    spec_model
        ``spec.claude.model`` — the model the agent was LAUNCHED on. It is
        the declared model, so it can lag a switch performed in the TUI; that
        lag is covered by ``already_switched`` and by leg 4 (a switched agent
        that took a turn has an advancing pane), not by trusting the spec.
    first, second
        Two readings of the SAME pane, an interval apart. The pair is what
        separates a parked agent from a working one.
    now
        The instant the SECOND capture was taken, timezone-aware. Present for
        symmetry with :func:`._rule.decide` and for the detail lines; this
        remedy deliberately does NOT gate on a reset time, because the
        measured Fable banner publishes none.
    already_switched
        The caller's memory that it already switched this agent and has not
        yet seen the result. See principle 3 in the module docstring.
    """
    del now  # carried for symmetry with the sibling rule; not a gate here.

    if policy not in MANAGED_POLICIES:
        return SwitchDecision(
            Verdict.NOT_MANAGED,
            "policy-never",
            f"restart.policy={policy!r} — sac never promised to keep {name} "
            f"running, so its model is not sac's to change",
        )

    if session_present is None:
        return SwitchDecision(
            Verdict.UNREADABLE,
            "sessions-unreadable",
            f"could not enumerate tmux sessions, so we do not know whether "
            f"{name} has one. Refusing to infer anything from a reading we "
            f"failed to take",
        )
    if not session_present:
        return SwitchDecision(
            Verdict.NO_SESSION,
            "no-session",
            f"{name} has no live tmux session — there is no pane to type "
            f"/model into. A corpse is `sac agents reconcile`'s",
        )

    if not (first.readable and second.readable):
        return SwitchDecision(
            Verdict.UNREADABLE,
            "pane-unreadable",
            f"{name}'s pane could not be captured on both reads "
            f"({second.detail or first.detail}) — no evidence, which is not "
            f"evidence that it is working",
        )

    if not second.capped:
        return SwitchDecision(
            Verdict.NOT_LIMITED,
            "no-model-cap",
            f"no model-cap banner on {name}'s pane — nothing here to switch "
            f"around",
        )

    if not first.capped or first.line_index != second.line_index:
        return SwitchDecision(
            Verdict.MOVING,
            "pane-advancing",
            f"{name}'s pane shows a model-cap banner but it MOVED between the "
            f"two reads (line {first.line_index} → {second.line_index}), so "
            f"the pane is still producing output. That is an agent working, "
            f"or one quoting the incident — never one parked behind a cap",
        )

    family = model_family(spec_model)
    target_family = model_family(target)
    if family and family == target_family:
        return SwitchDecision(
            Verdict.ALREADY_ON_TARGET,
            "already-on-target",
            f"{name} is already on {spec_model!r}, which is the "
            f"{target_family!r} family this switcher moves agents TO. Its cap "
            f"banner is a different problem and switching it to the model it "
            f"is already running would spend a real turn to achieve nothing",
        )

    if not family:
        return SwitchDecision(
            Verdict.NOT_LIMITED,
            "unknown-model-family",
            f"{name}'s spec names {spec_model or 'no model at all'!r}, whose "
            f"family sac cannot read, so the banner would be the ONLY evidence "
            f"— and a banner is corroboration, never authority. The provider's "
            f"cap sentence is carried VERBATIM in this repository (specimens, "
            f"docstrings, tests), so any agent that read one of those files and "
            f"then went idle has a matching line frozen on its pane, at the "
            f"same index on both reads. Acting on that alone would retype the "
            f"model of a local-model agent onto Anthropic Opus, which is the "
            f"one class where falling back to Claude is never wanted",
        )
    banner_family = model_family(second.subject)
    if family not in CAPPED_FAMILIES and banner_family not in CAPPED_FAMILIES:
        return SwitchDecision(
            Verdict.NOT_LIMITED,
            "not-a-capped-family",
            f"{name} is capped, but on {spec_model!r} "
            f"(family {family!r}) and the banner names "
            f"{second.subject!r} — neither is one of {CAPPED_FAMILIES}. This "
            f"remedy is 'get off the capped model'; it means nothing here, so "
            f"the rate-wall verdict stands",
        )

    if already_switched:
        return SwitchDecision(
            Verdict.COOLING_DOWN,
            "already-switched",
            f"{name} was ALREADY switched and its pane has not advanced "
            f"since, so the previous switch's outcome is not yet visible. "
            f"Firing again would type a second /model into a pane that may "
            f"still be showing a picker from the first",
        )

    return SwitchDecision(
        Verdict.SWITCH_MODEL,
        "capped-on-a-switchable-model",
        f"{name} is parked behind a frozen {second.subject!r} cap "
        f"({second.detail}) while running {spec_model!r}. This wall does not "
        f"need waiting out — it needs walking around, and the provider's own "
        f"banner names the way: switch to {target!r} and kick the session",
        target=target,
    )
