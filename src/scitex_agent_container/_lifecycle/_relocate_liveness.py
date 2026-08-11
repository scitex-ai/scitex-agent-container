"""Is the agent running on that host? Asked of tmux, answered in three values.

The transport's precondition. A running source appends to its ``.jsonl`` while
it is being read, and a jsonl truncated mid-line still parses and still resumes —
the conversation simply stops early, with nothing anywhere reporting a problem.
So this question has to be ASKED, and its unanswered form has to refuse.

WHY tmux AND NOT ``sac agents list``. Three reasons, in order of weight:

    tmux's answer is the same fact the runtime itself checks.
    :class:`..runtimes.tui_session.TuiSessionRuntime.is_running` looks for the
    session ``tui-<name>``; asking anything else introduces a second definition
    of running that can disagree with the one that matters.

    A tmux server that answered and holds no such session is POSITIVE EVIDENCE
    OF ABSENCE, from tmux's own bookkeeping. That is a different and much
    stronger thing than a query that failed, and the two must not collapse.

    It needs no venv, no PATH and no sac on the far host. The fewer things that
    have to be true for the measurement to happen, the fewer ways "I could not
    tell" gets mistaken for "it is not running".

NO tmux SERVER AT ALL is likewise positive absence, and is reported as such: an
agent whose runtime IS a tmux session cannot be running where there is no tmux
server. tmux not being INSTALLED is the opposite — nothing was measured — and
refuses. Those two look similar in a shell and mean opposite things.
"""

from __future__ import annotations

from typing import Callable

from ._relocate_execute import StepResult
from ._relocate_shell import Shell

__all__ = ["MARK_RUN", "observe_running", "unimplemented"]

#: Marker line, parsed rather than read off the exit code, so a shell that also
#: prints a warning cannot be mistaken for a measurement.
MARK_RUN = "TX-RUN="


def unimplemented(phase: str, missing: str) -> Callable[[], StepResult]:
    """An effect that refuses, naming the piece that does not exist yet.

    UNKNOWN rather than a failure, because nothing was attempted and therefore
    nothing about the hosts was learned — the same distinction every decision
    module in this feature makes, turned on our own absence. The driver requires
    an effect for every phase precisely so that "leave it out and let it pass"
    is unavailable; this is the honest thing to put there instead.
    """

    def effect() -> StepResult:
        return StepResult(
            ok=None,
            # Nothing was attempted, so nothing can have been left behind. The
            # driver reads this to decide whether to tell the operator a standby
            # may still be running; without it the recovery instruction sends
            # somebody to stop a process that was never started.
            attempted=False,
            detail=f"{phase} has no adapter: {missing}",
            hint=(
                f"build the {phase} adapter, or complete this phase by hand and re-run "
                "— the journal records where this stopped and a re-run resumes from here"
            ),
        )

    return effect


def observe_running(
    shell: Shell,
    agent: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> tuple[bool | None, str]:
    """``(running, why)`` for ``agent`` on ``shell``. ``None`` means not measured.

    The reason travels with the answer because every caller puts it in a journal
    entry or a refusal, and "could not determine" with no account of what was
    tried is not something anyone can act on.
    """
    session = f"tui-{agent}"
    body = (
        "if ! command -v tmux >/dev/null 2>&1; then "
        f"printf '{MARK_RUN}no-tmux\\n'; exit 0; fi\n"
        "if ! tmux list-sessions >/dev/null 2>&1; then "
        f"printf '{MARK_RUN}no-server\\n'; exit 0; fi\n"
        f"if tmux has-session -t '{session}' 2>/dev/null; then "
        f"printf '{MARK_RUN}yes\\n'; else printf '{MARK_RUN}no\\n'; fi"
    )
    run = shell.run(body, exec_fn=exec_fn)
    return interpret_liveness(
        [
            ln[len(MARK_RUN) :].strip()
            for ln in run.stdout.splitlines()
            if ln.startswith(MARK_RUN)
        ],
        host=shell.host,
        session=session,
        exit_code=run.exit_code,
        stderr=run.stderr,
    )


def interpret_liveness(
    answers: list[str],
    *,
    host: str,
    session: str,
    exit_code: int,
    stderr: str,
) -> tuple[bool | None, str]:
    """Turn the probe's marker lines into a verdict. Pure, so it is testable."""
    answer = answers[0] if answers else ""
    if answer == "yes":
        return True, f"tmux on {host} has session {session}"
    if answer == "no":
        return False, (
            f"tmux on {host} answered and has NO session {session} — positive evidence "
            "of absence, from tmux's own bookkeeping"
        )
    if answer == "no-server":
        return False, (
            f"no tmux server is running on {host}, so a tui-runtime agent cannot be "
            "running there"
        )
    if answer == "no-tmux":
        return None, f"tmux is not installed on {host}; liveness was not measured"
    return None, (
        f"the liveness probe on {host} produced no answer (exit {exit_code}): "
        f"{stderr.strip()[:160]}"
    )


__all__.append("interpret_liveness")
