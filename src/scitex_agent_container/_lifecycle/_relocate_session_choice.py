"""WHICH conversation moves, when the directory holds several. Chosen, never guessed.

An agent's project directory holds one ``.jsonl`` per session it has ever run, and
the relocation carries all of them. Exactly one of them is the LIVE conversation —
the one the target must be seeded to resume — and until 2026-08-12 the transport
identified it by a guard that read, in full::

    if len(plan.files) == 1:
        self.session_uuid = plan.files[0].rsplit(".", 1)[0]

MEASURED THE SAME NIGHT, ON ywata-note-win: not one of the ten agents left to move
has exactly one transcript (3, 4, 4, 5, 3, 2, 3, 2, 4, 4). Every one of them
returned ``GO — every check passed (11 checks)`` from preflight and then could not
complete: ``source_stop`` stopped the agent, TRANSPORT verified every byte, and
TARGET_STANDBY refused with "the carried session id is not known" — after the
agent was already down. Confirmed live on scitex-clew: three transcripts,
10,243,042 / 4,722,667 / 30,698,234 bytes all verified, then ``phase='aborted'``,
no marker seeded, target never started, source left stopped.

THE ORDER OF PREFERENCE, AND WHY EACH STEP IS WHERE IT IS.

1. THE SOURCE'S OWN MARKER. ``runtime/<agent>/session_id`` is what the source's
   runtime itself last resumed. It is not an inference about which file looks
   live; it is the answer, written by the thing that knows.

2. THE MOST RECENTLY MODIFIED CARRIED TRANSCRIPT. Only when there is no marker at
   all. The live conversation is the one that was being written, so mtime orders
   them — but it is second because it REASONS about the files where the marker
   REPORTS about the agent.

NEITHER RESOLVING IS A REFUSAL THAT NAMES THE CANDIDATES. A marker pointing at a
file that was not carried means the transport and the runtime disagree about which
conversation this agent is having, and picking either one silently would start the
target on an undefined session — the one failure a byte count cannot catch, and
the reason ``start_standby`` names the session by id rather than saying "continue".
Two files sharing the newest mtime is the same situation and gets the same answer.

Pure. Names and timestamps in, a choice or a refusal out — so every path is a test
with real values, and the same function answers at PREFLIGHT (before anything is
stopped) and at TRANSPORT.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Sequence

__all__ = [
    "CODE_AMBIGUOUS",
    "CODE_CHOSEN",
    "CODE_MARKER_NOT_CARRIED",
    "CODE_NO_CANDIDATES",
    "CODE_UNKNOWN",
    "SessionChoice",
    "choose_session",
    "session_id_for",
    "transcript_name_for",
]

#: One session was identified, and by what.
CODE_CHOSEN: Final = 200
#: Several candidates and nothing to choose between them.
CODE_AMBIGUOUS: Final = 300
#: Nothing was carried, so there is no conversation to resume.
CODE_NO_CANDIDATES: Final = 404
#: The runtime's marker names a session whose transcript did not travel.
CODE_MARKER_NOT_CARRIED: Final = 409
#: Something was not observed. Refuses as firmly as a failure, differently.
CODE_UNKNOWN: Final = 503

TRANSCRIPT_SUFFIX: Final = ".jsonl"


def session_id_for(name: str) -> str:
    """The session id a transcript file name carries."""
    base = name.rsplit("/", 1)[-1]
    return base[: -len(TRANSCRIPT_SUFFIX)] if base.endswith(TRANSCRIPT_SUFFIX) else base


def transcript_name_for(session: str) -> str:
    """The transcript file name a session id would be stored in."""
    return f"{session}{TRANSCRIPT_SUFFIX}"


@dataclass(frozen=True)
class SessionChoice:
    """Which session travels, or why one could not be named.

    ``session`` is ``None`` when nothing was chosen; there is deliberately no
    ``__bool__``, because the next thing that happens with a choice is a marker
    written onto another machine and a boot that resumes it.

    ``candidates`` always carries what was actually seen, including on success —
    a choice among four that reports only the winner cannot be reviewed.
    """

    session: str | None
    code: int
    reason: str
    chosen_by: str = ""
    candidates: tuple[str, ...] = ()
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("SessionChoice.reason must be non-empty")
        if self.session is not None and self.code != CODE_CHOSEN:
            raise ValueError(
                f"SessionChoice: a chosen session must carry CODE_CHOSEN, got {self.code}"
            )
        if self.session is not None and not self.chosen_by:
            raise ValueError(
                "SessionChoice: a chosen session must say WHAT chose it — 'the marker' "
                "and 'the newest file' are different levels of evidence and a reader "
                "must be able to tell which one they are trusting"
            )
        if self.session is None and not self.hint:
            raise ValueError(
                "SessionChoice: a refusal must say what to do next — an unresolved "
                "session stops a relocation, and the operator is the one who resolves it"
            )


def _listed(candidates: Sequence[str]) -> str:
    return ", ".join(candidates) if candidates else "(none)"


def choose_session(
    *,
    agent: str,
    carried: Sequence[str],
    marker: str | None,
    mtimes: Mapping[str, int | None] | None = None,
) -> SessionChoice:
    """Name the conversation to resume, or refuse naming what was seen.

    ``carried`` are the transcript FILE NAMES that will travel. ``marker`` is the
    source's ``runtime/<agent>/session_id``: a session id, ``""`` for LOOKED AND
    FOUND NOTHING, or ``None`` for NOBODY LOOKED — three answers, kept apart,
    because an unread marker could name a different file than the newest one and
    is therefore not the same as an absent marker. ``mtimes`` maps each carried
    name to its modification time in epoch seconds, and is only consulted when
    there is no marker and more than one candidate.
    """
    names = tuple(carried)
    if not names:
        return SessionChoice(
            session=None,
            code=CODE_NO_CANDIDATES,
            reason=(
                f"no transcript was selected for {agent}, so there is no conversation "
                "to resume on the target"
            ),
            hint=(
                "check the source's project directory and the workdir encoding before "
                "moving. An agent started with no session resumes nothing, which is the "
                "2026-08-07 failure: it boots, reports healthy, and has no memory"
            ),
        )

    if marker:
        wanted = transcript_name_for(marker)
        if wanted in names:
            return SessionChoice(
                session=marker,
                code=CODE_CHOSEN,
                reason=(
                    f"{agent}'s own runtime marker names session {marker}, and "
                    f"{wanted} is among the {len(names)} transcript(s) being carried"
                ),
                chosen_by="the source's runtime session marker",
                candidates=names,
            )
        return SessionChoice(
            session=None,
            code=CODE_MARKER_NOT_CARRIED,
            reason=(
                f"{agent}'s runtime marker names session {marker}, whose transcript "
                f"{wanted} is NOT among what would be carried: {_listed(names)}"
            ),
            candidates=names,
            hint=(
                "the runtime and the transport disagree about which conversation this "
                f"agent is having. Check that {wanted} exists in the source's project "
                "directory (the workdir encoding is the first thing to look at), or "
                "correct the marker. Do not let it pick one — a target seeded with the "
                "wrong session resumes a conversation nobody asked for, and no byte "
                "count catches it"
            ),
        )

    if len(names) == 1:
        only = names[0]
        return SessionChoice(
            session=session_id_for(only),
            code=CODE_CHOSEN,
            reason=(
                f"{only} is the only transcript being carried, so it is the "
                "conversation to resume"
            ),
            chosen_by="the only carried transcript",
            candidates=names,
        )

    if marker is None:
        return SessionChoice(
            session=None,
            code=CODE_UNKNOWN,
            reason=(
                f"{agent}'s runtime session marker was not read, and there are "
                f"{len(names)} candidates: {_listed(names)}"
            ),
            candidates=names,
            hint=(
                "read runtime/<agent>/session_id on the source before deciding. An "
                "unread marker is not an absent one — it may well name a different "
                "file from the most recent, and that is precisely the case where "
                "guessing is wrong"
            ),
        )

    times = {n: (mtimes or {}).get(n) for n in names}
    unmeasured = sorted(n for n, t in times.items() if t is None)
    if unmeasured:
        return SessionChoice(
            session=None,
            code=CODE_UNKNOWN,
            reason=(
                f"{agent} has no session marker and the modification time of "
                f"{_listed(tuple(unmeasured))} was not measured, so the most recent of "
                f"{len(names)} candidates cannot be identified"
            ),
            candidates=names,
            hint=(
                "measure the mtimes on the source and try again, or seed "
                "runtime/<agent>/session_id there with the session that should travel"
            ),
        )

    newest = max(times.values())
    tied = sorted(n for n, t in times.items() if t == newest)
    if len(tied) > 1:
        return SessionChoice(
            session=None,
            code=CODE_AMBIGUOUS,
            reason=(
                f"{agent} has no session marker and {len(tied)} transcripts share the "
                f"newest modification time ({newest}): {_listed(tuple(tied))}"
            ),
            candidates=names,
            hint=(
                "seed runtime/<agent>/session_id on the source with the session that "
                "should travel, then re-run. Picking either of a tie would start the "
                "target on an undefined conversation"
            ),
        )

    chosen = tied[0]
    return SessionChoice(
        session=session_id_for(chosen),
        code=CODE_CHOSEN,
        reason=(
            f"{agent} has no runtime session marker; {chosen} is the most recently "
            f"modified of {len(names)} carried transcript(s)"
        ),
        chosen_by="the most recently modified carried transcript",
        candidates=names,
    )
