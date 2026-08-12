"""HANDSHAKE: deliver the brief, then go and look for the answer it demanded.

:mod:`_relocate_handshake` decides whether what came back proves anything, and
:mod:`_relocate_arrival` writes the brief that IS the challenge. Both were built
and neither was ever delivered. This module is the leg between them.

WHERE THE ANSWER IS SOUGHT, AND WHAT THAT HONESTLY PROVES. The challenge is
POSTed to the agent's own sidecar on its own host; the answer is read out of the
agent's TRANSCRIPT, and those bytes then travel to the source, where the
coordinator parses them. So the coordinator ON THE SOURCE observed the reply.
A network path from the target back to the source is NOT what was measured, and
``observed_by`` says so in those words rather than in words that would let a
reader assume the stronger thing.

WHY NOT SIMPLY READ THE SEND'S REPLY. Because for a ``runtime: tui`` agent there
isn't one: the host-side turn bridge injects the text into tmux and answers
``{"text": "", "delivered": true}`` by design (see
:mod:`..runtimes._tui_turn_bridge`). A synchronous send therefore proves
DELIVERY and nothing else — which is precisely the accepted-but-silent shape
measured on 2026-08-11, and precisely what this gate exists to refuse. The
answer is sought somewhere the agent had to actually write it.

WHAT THIS DOES PROVE is the part the gate was written for. To produce the
answer, the agent's loop had to take a turn, run a command whose result it could
not know without looking, and write the result down. "Started, reported healthy,
did nothing" cannot pass that.

THE CHALLENGE IS IN THE TRANSCRIPT TOO, and it nearly makes this search lie: the
brief we injected contains the line ``nonce=…`` because it must — it is telling
the agent what to send back. :mod:`_relocate_reply` is the parser that tells the
question from the answer, by the angle-bracket placeholder the brief writes and
a real answer never has.

THE EXPECTED ANSWER IS MEASURED, NOT ASSUMED. The question asks the agent to run
``hostname``, so the answer it is checked against comes from running ``hostname``
on the target. Substituting the fleet's name for that host would fail a healthy
relocation while reporting the target's loop as broken — the two names differ
often enough that assuming is a real risk, not a theoretical one.

UNKNOWN STAYS UNKNOWN. A delivery that could not RUN, or a search that never
completed, teaches nothing about the agent and is reported as NOT OBSERVED. Only
a challenge that was accepted and a search that ran to the end of its patience
may be called "no reply" — anything else would accuse a target that was never
asked anything.
"""

from __future__ import annotations

import secrets
import time

from ._relocate_arrival import build_arrival_brief
from ._relocate_execute import StepResult
from ._relocate_handshake import HandshakeFacts, evaluate_handshake
from ._relocate_reply import read_reply
from ._relocate_target_ssh import deliver_prompt, search_transcripts, target_hostname

__all__ = [
    "HANDSHAKE_QUESTION",
    "REPLY_ATTEMPTS",
    "REPLY_INTERVAL_S",
    "HandshakeEffects",
]

#: The proof-of-work question. It must be answerable ONLY by looking at the
#: machine, so a loop that runs without working tools cannot produce it, and it
#: must be cheap, so a busy agent is not failed for being slow.
HANDSHAKE_QUESTION = (
    "run `hostname` on the machine you are on now and report it verbatim"
)

#: How long to keep looking, and how often. An agent resuming a multi-megabyte
#: transcript must load it before it can take a turn, so the patience is real —
#: five minutes, checked every ten seconds. Being impatient here does not fail
#: safe: it produces a "no reply" verdict about an agent that was still reading.
REPLY_ATTEMPTS = 30
REPLY_INTERVAL_S = 10.0


class HandshakeEffects:
    """Mixin: the HANDSHAKE phase. Expects ``RelocateAdapters``' attributes."""

    def handshake(self) -> StepResult:
        """Deliver the brief and observe the answer, or say what was not observed."""
        if not self.session_uuid or not self.target_dir:
            return StepResult(
                ok=None,
                attempted=False,
                detail=(
                    "the carried session id or the target transcript directory is not "
                    "known, so the brief cannot state the handle the agent needs and "
                    "there is nowhere to read its answer"
                ),
                hint="re-run from the transport phase, which establishes both",
            )
        expected = target_hostname(self.target, exec_fn=self.exec_fn)
        if not expected:
            return StepResult(
                ok=None,
                attempted=False,
                detail=(
                    f"{self.to_host} did not answer `hostname`, so there is no "
                    "independently computed answer to check a reply against"
                ),
                hint=(
                    "measure it on the target. Substituting the fleet's name for the host "
                    "would fail a healthy agent whose machine calls itself something else"
                ),
            )
        nonce = secrets.token_hex(8)
        brief = build_arrival_brief(
            agent=self.agent,
            from_host=self.from_host,
            to_host=self.to_host,
            resume_session_id=self.session_uuid,
            nonce=nonce,
            question=HANDSHAKE_QUESTION,
        )
        self.log.append(
            f"handshake: brief built ({len(brief)} chars), nonce {nonce}, expected "
            f"answer {expected!r} (measured on {self.to_host}, not assumed from its fleet name)"
        )

        accepted = self._deliver(brief)
        facts = HandshakeFacts(challenge_accepted=accepted)
        if accepted is True:
            observed, who, seen_nonce, answer = self._observe_reply(nonce)
            facts = HandshakeFacts(
                challenge_accepted=True,
                reply_observed=observed,
                observed_by=who,
                reply_nonce=seen_nonce,
                reply_answer=answer,
            )

        verdict = evaluate_handshake(facts, nonce=nonce, expected_answer=expected)
        self.handshake_confirmed = verdict.proven
        if verdict.proven is not True:
            return StepResult(
                ok=False if verdict.proven is False else None,
                detail=f"handshake not proven ({verdict.code}): {verdict.reason}",
                hint=verdict.hint,
            )
        return StepResult(ok=True, detail=verdict.reason)

    def _deliver(self, brief: str) -> bool | None:
        """POST the challenge to the agent's sidecar on its own host.

        ``None`` when the command could not be RUN at all — NOT OBSERVED, which
        the gate refuses on without accusing the target of silence. Reporting
        "no reply" for a challenge that was never sent would blame an agent
        nothing ever reached.
        """
        try:
            run = deliver_prompt(self.target, self.agent, brief, exec_fn=self.exec_fn)
        except Exception as exc:  # stx-allow: fallback (reason: a delivery that could not RUN is NOT OBSERVED; converting it into "the target refused" would accuse an agent nothing reached)
            self.log.append(
                f"handshake: delivery could not run — {type(exc).__name__}: {exc}"
            )
            return None
        self.log.append(
            f"handshake: `sac agents send` on {self.to_host} exit {run.exit_code} — "
            f"{run.stdout.strip()[:300]}"
        )
        return run.exit_code == 0

    def _observe_reply(self, nonce: str):
        """Look for the answer, patiently. Returns the four handshake facts.

        Returns ``(reply_observed, observed_by, reply_nonce, reply_answer)``.
        ``reply_observed`` is three-valued and the distinction is the whole
        point: ``False`` only after a search that actually RAN for the full
        window and found the challenge but no answer; ``None`` when every look
        failed to run, which says nothing about the agent.
        """
        who = (
            f"the coordinator on {self.from_host}, reading {self.agent}'s own transcript "
            f"on {self.to_host} over ssh (the target did not dial back — that path is not "
            "what this measured)"
        )
        searched = False
        for attempt in range(REPLY_ATTEMPTS):
            if attempt:
                time.sleep(REPLY_INTERVAL_S)
            lines = search_transcripts(
                self.target, self.target_dir, nonce, exec_fn=self.exec_fn
            )
            if lines is None:
                continue
            searched = True
            reading = read_reply("\n".join(lines), nonce=nonce)
            if reading.answer:
                self.log.append(
                    f"handshake: answer observed after {attempt + 1} look(s) — "
                    f"{reading.answer!r}; ignored {reading.placeholders_ignored} "
                    "occurrence(s) of the challenge's own placeholder"
                )
                return True, who, nonce if reading.nonce_seen else None, reading.answer
        if not searched:
            self.log.append(
                f"handshake: the transcript search on {self.to_host} never ran "
                "successfully — nothing was learned about the agent"
            )
            return None, who, None, None
        waited = REPLY_ATTEMPTS * REPLY_INTERVAL_S
        self.log.append(
            f"handshake: searched {self.to_host}:{self.target_dir} for {waited:.0f}s and "
            "found the challenge but no answer to it"
        )
        return False, who, None, None
