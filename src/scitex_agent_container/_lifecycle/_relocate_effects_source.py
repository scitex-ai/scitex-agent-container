"""The two phases that happen on the host being LEFT: drain, then stop.

Split from :mod:`_relocate_effects` because they are the only steps whose subject
is the source, and because their shared rule is worth stating once: BOTH are
gated on an OBSERVED liveness answer, and an unobserved one refuses. A live agent
appends to its transcript while it is being read, and a jsonl truncated mid-line
still parses and still resumes — the conversation simply stops early, with
nothing anywhere reporting a problem. That is the failure the whole ordering of
this feature was rearranged to prevent, so it is not something to be optimistic
about.

DRAIN AND STOP ARE NOT THE SAME STEP, and collapsing them is the tempting
shortcut. A stop mid-turn loses the turn — which is exactly the work a relocation
exists to carry across. So for a RUNNING agent the drain refuses by name rather
than quietly doing a stop and calling it drained; for a STOPPED one it is
vacuously satisfied, and even that is MEASURED rather than assumed.
"""

from __future__ import annotations

from ._relocate_execute import StepResult
from ._relocate_liveness import observe_running

__all__ = ["SourceEffects"]


class SourceEffects:
    """Mixin: the source-side phases. Expects the attributes of ``RelocateAdapters``."""

    def drain_source(self) -> StepResult:
        """Confirm the source is taking no new work.

        FOR A STOPPED AGENT THIS IS VACUOUSLY TRUE, and it is measured rather
        than assumed: an agent with no session cannot accept work. For a RUNNING
        one there is no adapter — telling an agent to finish what it holds, take
        nothing new, and confirming it did, is an agent-protocol question that
        cannot be answered by looking at a process.
        """
        running, why = observe_running(self.source, self.agent, exec_fn=self.exec_fn)
        if running is False:
            return StepResult(ok=True, detail=f"nothing to drain — {why}")
        if running is None:
            return StepResult(
                ok=None,
                detail=f"whether {self.agent} runs on {self.from_host} was not measured: {why}",
                hint=(
                    "measure liveness on the source before draining; an unobserved 'is it "
                    "running' is the precondition the transport depends on"
                ),
            )
        return StepResult(
            ok=None,
            attempted=False,
            detail=(
                f"{self.agent} is RUNNING on {self.from_host} and there is no drain "
                "adapter: nothing here can tell an agent to finish its in-flight work, "
                "take no new work, and confirm that it did"
            ),
            hint=(
                "drain it by hand (let the turn finish, stop dispatching to it), then "
                "re-run. Do NOT substitute a stop for a drain — a stop mid-turn loses "
                "the turn, which is exactly the work a relocation exists to carry"
            ),
        )

    def stop_source(self) -> StepResult:
        """Stop the agent on the source host and VERIFY it stopped.

        Idempotent: an already-stopped agent is a success with nothing done. The
        verification is a SECOND, independent observation after the stop, because
        ``sac agents stop`` exiting 0 is the command's opinion and the
        transport's precondition is the tmux server's.
        """
        running, why = observe_running(self.source, self.agent, exec_fn=self.exec_fn)
        if running is False:
            return StepResult(ok=True, detail=f"already stopped — {why}")
        if running is None:
            return StepResult(
                ok=None,
                detail=f"could not tell whether {self.agent} runs on {self.from_host}: {why}",
                hint=(
                    "measure it before copying. A live agent appends to its transcript "
                    "mid-read, and a jsonl truncated mid-line still parses and still "
                    "resumes — the conversation just stops early"
                ),
            )

        run = self.source.run(
            f"sac agents stop {self.agent} --json 2>&1 || true", exec_fn=self.exec_fn
        )
        self.log.append(f"stop_source: {run.stdout.strip()[:300]}")
        after, after_why = observe_running(
            self.source, self.agent, exec_fn=self.exec_fn
        )
        if after is False:
            return StepResult(ok=True, detail=f"stopped and verified — {after_why}")
        if after is None:
            return StepResult(
                ok=None,
                detail=f"the stop was issued and the result was not observable: {after_why}",
                hint="re-observe liveness on the source; do not copy until it reads stopped",
            )
        return StepResult(
            ok=False,
            detail=f"the stop was issued and {self.agent} is STILL running on {self.from_host}",
            hint=(
                "stop it by hand and confirm, then re-run. Copying now would read a "
                "transcript that is being appended to"
            ),
        )
