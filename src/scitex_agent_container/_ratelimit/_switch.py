"""The OTHER mutation: move a capped agent onto the target model, then kick it.

Separated from the pass so the pass stays a decision loop and this stays the
irreversible act — the same split :mod:`._resume` makes, for the same reason.
Sibling of :mod:`._resume`, and the difference between them is the whole point
of this package's newest branch: ``_resume`` continues an agent whose wall came
down on its own; this one walks an agent around a wall that has not.

THE OPERATOR'S OWN MECHANISM, implemented literally
---------------------------------------------------
2026-09-06, verbatim in substance: *"1. /model opus[1m] needed  2. Enter or "1"
needed to confirm  3. kick needed after the model switch fixed"*, and *"between
the three steps, i think we should place three seconds for safety"*.

So: THREE sends, in that order, :data:`SWITCH_STEP_DELAY_S` apart. Not two and
not four. The gap is not decoration — the 3 s between step 1 and step 2 is
also, by luck and by physics, exactly the settle that
:meth:`.._runners._tmux.tmux.TmuxManager.send_text_and_submit` exists to
provide: the containerized Ink/React TUI EATS an ``Enter`` fired while it is
still re-rendering the text that was just pasted, which is the failure mode
this fleet has already paid for on the boot path.

WHY NOT ``_delivery.deliver`` FOR STEPS 1 AND 2 — measured, not assumed
-----------------------------------------------------------------------
:mod:`._resume` sends its nudge through :func:`.._delivery._deliver.deliver`
and says why: an unverified ``tmux send-keys`` leaves a prompt sitting unsent
at the prompt marker, which is indistinguishable from the outage. That
reasoning is right and it still holds — for PROSE.

It cannot carry a SLASH COMMAND, and the reason is one line of that module's
own code: ``deliver`` calls ``_token.format_payload(message, token)``
unconditionally, which returns ``f"[sac-deliver:{token}] {message}"``. The
payload therefore reaches the composer as ``[sac-deliver:a1b2c3] /model
opus[1m]`` — text whose first character is ``[``, not ``/``. A slash command
is only a command when the slash is the first thing in the box; anywhere else
it is prose, and the TUI would submit it as a question about a model rather
than as an instruction to change one. There is no flag to suppress the token
(it is the arrival matcher's whole mechanism), so steps 1 and 2 go through the
package's tmux helper directly.

Step 3 — the kick — IS prose, so it goes through ``deliver`` exactly as
``_resume`` does, and inherits the three-valued proof that the payload left
the compose box. That proof is then EVIDENCE in the verification below, not a
verdict: see :func:`.._modelcap.verify_switch`.

NOTHING HERE ASSUMES A SEND THAT RETURNED 0 DID ANYTHING
--------------------------------------------------------
``tmux send-keys`` exiting 0 means tmux accepted a keystroke. It does not mean
a model changed, and a switcher that reported success on that basis would hand
the operator a fleet he believes recovered. So the last thing this module does
is CAPTURE THE PANE AGAIN and ask :func:`.._modelcap.verify_switch` what the
screen now shows, three-valued: switched / not-switched / unknown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from ._modelcap import verify_switch
from ._switch_rule import TARGET_MODEL

__all__ = [
    "KICK_MESSAGE",
    "SWITCH_STEP_DELAY_S",
    "SwitchOutcome",
    "SwitchStep",
    "model_command",
    "real_switch",
    "switch_model_now",
]

#: Seconds between the operator's three steps. His number, his words: *"between
#: the three steps, i think we should place three seconds for safety"*. Kept as
#: a named constant rather than inlined so a future measurement can move it in
#: one place — and so a test can prove the spacing was honoured without racing
#: a real clock.
SWITCH_STEP_DELAY_S = 3.0

#: The confirm keystroke. The operator's step 2 is *"Enter or \"1\""*: when
#: ``/model <target>`` applies the model directly there is nothing to confirm
#: and this Enter submits an empty composer (a harmless no-op); when the TUI
#: instead opens its model picker, this is the keystroke that accepts the
#: highlighted row. One key covers both renderings, which is why it is the one
#: sent.
CONFIRM_KEY = "Enter"

#: What the switched agent is told. Prose, so it travels the VERIFIED delivery
#: path. Deliberately about the agent's OWN state rather than a new
#: instruction: this enforcer knows a model was capped and knows nothing
#: whatever about what the agent was doing, so pointing it back at its own
#: board is the only direction it is entitled to give.
#:
#: It says ``the 1M-context Opus`` rather than ``opus[1m]`` on purpose. The
#: verifier counts occurrences of the target id on the pane and subtracts the
#: ones sac itself put there; writing the id verbatim here would add another
#: copy to the screen for no gain. (The text is handed to the verifier as a
#: sent-text and only counted when it is actually rendered, so correctness
#: does not depend on this wording — only tidiness does. Note that a naive
#: "Opus (1M context)" would have flattened to the target id EXACTLY, which
#: is how close this trap sits to the surface.)
KICK_MESSAGE = (
    "sac.resume-rate-limited-agents switched this session OFF the Fable model, "
    "which had run out of quota and was answering nothing, and ONTO the "
    "1M-context Opus. You were paused, not restarted — your context is "
    "intact. Re-read your own scitex-cards board and continue the work that "
    "was interrupted; if nothing is outstanding, say so and stop."
)

def _session_for(agent: str) -> str:
    """``tui-<agent>`` — the DEFAULT tmux server's session for this agent.

    The prefix is imported from the delivery router rather than spelled a
    second time here: that module carries the measured warning that the
    ``-L sac`` server is a DIFFERENT server whose emptiness would read as
    this fleet's death.
    """
    from .._delivery._route import TUI_SESSION_PREFIX

    return f"{TUI_SESSION_PREFIX}{agent}"


def model_command(target: str = TARGET_MODEL) -> str:
    """The literal step-1 keystroke text: ``/model <target>``.

    A function and not an f-string at the call site so the ONE place that
    decides what gets typed into an operator's agent is greppable.
    """
    return f"/model {target}"


@dataclass(frozen=True)
class SwitchStep:
    """One send, and WHEN it happened on the injected clock.

    ``at`` is what a test reads to prove the operator's 3-second spacing was
    honoured without any test having to sleep for nine seconds.
    """

    name: str
    payload: str
    at: float


@dataclass(frozen=True)
class SwitchOutcome:
    """What the three steps did, and what the pane said afterwards.

    ``switched`` is THREE-VALUED and is the only field a caller may treat as a
    verdict: ``True`` proven, ``False`` proven otherwise, ``None`` we cannot
    tell. A ``None`` is reported as ``SWITCH-UNVERIFIED`` and exits 2 — an
    ambiguity costs a human a look, never the operator a silent agent he
    believes was recovered.
    """

    switched: bool | None
    detail: str
    steps: tuple[SwitchStep, ...] = ()
    kick_submitted: bool | None = None
    target: str = ""
    sent: tuple[str, ...] = field(default_factory=tuple)


def _real_kick(agent: str, message: str) -> bool | None:
    """Deliver the kick through the VERIFIED path. Three-valued, on purpose.

    ``True`` only on a PROVEN submission; ``False`` is "the payload is still
    sitting unsent"; ``None`` is "we could not tell". The last two are not the
    same and this module does not collapse them, because the verifier
    downstream treats a proven submission as evidence and everything else as
    silence.
    """
    from .._delivery._assess import assess_delivery
    from .._delivery._deliver import deliver

    return assess_delivery(deliver(agent, message)).verdict


def _default_paste(session: str, text: str) -> None:
    from .._delivery._tui_strategy import default_paste

    default_paste(session, text)


def _default_send_keys(session: str, key: str) -> None:
    from .._delivery._tui_strategy import default_send_keys

    default_send_keys(session, key)


def _default_capture(session: str) -> str | None:
    from .._delivery._tui_strategy import default_capture

    return default_capture(session)


def switch_model_now(
    agent: str,
    *,
    target: str = TARGET_MODEL,
    step_delay_s: float = SWITCH_STEP_DELAY_S,
    kick_message: str = KICK_MESSAGE,
    paste_fn: Callable[[str, str], None] = _default_paste,
    send_keys_fn: Callable[[str, str], None] = _default_send_keys,
    kick_fn: Callable[[str, str], bool | None] = _real_kick,
    capture_fn: Callable[[str], str | None] = _default_capture,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
    default_tz: timezone = timezone.utc,
) -> SwitchOutcome:
    """Perform the operator's three steps, then PROVE what the pane shows.

    Every collaborator is an injected seam with a REAL production default, so
    a test drives this function itself — not a rehearsal of it — by passing
    plain callables with the same signatures. Nothing is mocked and nothing
    sleeps in a test: ``sleep_fn`` and ``clock_fn`` are the same seam pair the
    delivery package uses.

    The sequence, and what each part buys:

    1. **paste** ``/model <target>`` literally (``send-keys -l`` — without it
       the Ink TUI silently drops the keystrokes and the pane stays
       byte-identical).
    2. **confirm** with a separate named ``Enter`` — never ``-l``, which would
       type the five characters "Enter" into the box.
    3. **kick** with prose, through the verified delivery path, so the
       submission is proven rather than hoped for.

    …then one more capture and :func:`.._modelcap.verify_switch`, whose answer
    is this function's return value. The delay is honoured BEFORE each of
    steps 2 and 3 and once more before the verifying capture, so the screen
    being judged is the settled one.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    session = _session_for(agent)
    command = model_command(target)
    steps: list[SwitchStep] = []

    steps.append(SwitchStep("model-command", command, clock_fn()))
    paste_fn(session, command)

    sleep_fn(step_delay_s)
    steps.append(SwitchStep("confirm", CONFIRM_KEY, clock_fn()))
    send_keys_fn(session, CONFIRM_KEY)

    sleep_fn(step_delay_s)
    steps.append(SwitchStep("kick", kick_message, clock_fn()))
    kick_submitted = kick_fn(agent, kick_message)

    sleep_fn(step_delay_s)
    steps.append(SwitchStep("verify", session, clock_fn()))
    evidence = verify_switch(
        capture_fn(session),
        target_model=target,
        sent_texts=(command, kick_message),
        kick_submitted=kick_submitted,
        now=moment,
        default_tz=default_tz,
    )
    return SwitchOutcome(
        switched=evidence.switched,
        detail=evidence.detail,
        steps=tuple(steps),
        kick_submitted=kick_submitted,
        target=target,
        sent=(command, kick_message),
    )


def real_switch(agent: str, target: str = TARGET_MODEL) -> bool | None:
    """The pass's default seam: switch ONE local agent, three-valued.

    The narrow signature the pass wires in, mirroring
    :func:`._resume.real_resume` — except that this one returns ``None`` as
    well as ``True``/``False``, because "we could not prove it" is a real
    outcome here and collapsing it into ``False`` would report a working agent
    as a failed switch (and, worse, invite a second one).
    """
    return switch_model_now(agent, target=target).switched
