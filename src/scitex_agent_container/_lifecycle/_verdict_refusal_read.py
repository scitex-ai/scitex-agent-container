"""READ one agent's transcript: did its most recent turn actually RUN?

The evidence half of the refusal instrument. :mod:`._verdict_refusal` holds the
full incident history and turns these reads into a
:class:`.._lifecycle._verdict_instruments.Signal`; this module does the reading
and the classifying, and is pure enough to unit-test against real transcript
files with no tmux, no clock of its own and no mocks.

WHAT IS BEING READ, AND WHY IT IS THE RIGHT ARTEFACT
    Claude Code stamps a turn the provider REFUSED with ``isApiErrorMessage:
    true``, ``model: "<synthetic>"`` and zero token usage — no turn ran. The
    failure writes that record itself, at the moment it happens, so it is the
    fault's own receipt rather than a proxy for it. A structural flag also
    cannot be spoofed by prose: an agent DISCUSSING an auth incident writes an
    ordinary turn with real tokens and no flag, which is the false-positive
    class that made every banner-matching detector in this repo dangerous
    (:mod:`.._authheal` restarted live agents 167 times in 7 days on one).

THE TWO WAYS A REFUSAL STOPS MEANING "UNABLE NOW"
    1. **The agent recovered.** Then a real turn follows it, and the LAST turn
       is not a refusal. Ordering does the work that
       :mod:`.._authheal._positional` had to approximate with pixel positions
       relative to a startup marker, because a transcript — unlike a screen —
       records sequence.
    2. **The agent stopped being asked.** A refusal that is still the last turn
       but is OLD means nobody has prompted it since; idle and unable are
       indistinguishable from here. That is :data:`STATE_UNKNOWN`, gated by
       ``stale_after_s``, mirroring the STALE branch of
       :func:`._verdict_screen.screen_signal`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CAUSE_CREDENTIALS",
    "CAUSE_QUOTA",
    "CAUSE_UNCLASSIFIED",
    "DEFAULT_STALE_AFTER_S",
    "DEFAULT_TAIL_BYTES",
    "REFUSAL_FLAG",
    "STATE_CLEAN",
    "STATE_REFUSED",
    "STATE_UNKNOWN",
    "SYNTHETIC_MODEL",
    "RefusalRead",
    "classify_refusal",
    "find_transcript",
    "last_turn_refusal",
]

#: The key Claude Code stamps on a turn the provider refused. Structural, not
#: textual — which is why prose can never match it.
REFUSAL_FLAG = "isApiErrorMessage"

#: The model string on a refused turn. Corroborates :data:`REFUSAL_FLAG`; a real
#: turn names a real model.
SYNTHETIC_MODEL = "<synthetic>"

#: Three-valued by construction. There is deliberately no "able": a last turn
#: that SUCCEEDED proves the agent could act THEN, not that it can act now — the
#: same restraint :func:`._verdict_screen.screen_signal` practises with a clean
#: pane. CLEAN means "no refusal seen", the absence of a known fault.
STATE_REFUSED = "refused"
STATE_CLEAN = "clean"
STATE_UNKNOWN = "unknown"

#: Causes. Named, because the constitution requires a failure to say WHICH and
#: WHAT NEXT — and because the two have OPPOSITE remedies: restarting a
#: quota-dead agent destroys its context and fixes nothing.
CAUSE_QUOTA = "quota-exhausted"
CAUSE_CREDENTIALS = "credentials-expired"
CAUSE_UNCLASSIFIED = "refusal-unclassified"

#: A refusal older than this is not a verdict — see the module docstring.
DEFAULT_STALE_AFTER_S = 900.0

#: Tail of the transcript to read. A refused turn is the LAST record, and these
#: files reach 117 MB on a long-lived agent (measured on the host 2026-08-12),
#: so reading the whole file on every health check is not an option.
DEFAULT_TAIL_BYTES = 64 * 1024

# Cause matchers. Substring tests against text we have ALREADY established is a
# refusal (by the structural flag), so they carry none of the false-positive
# risk the same strings have against arbitrary pane text.
_QUOTA_MARKERS = (
    "weekly limit",
    "usage limit",
    "rate limit",
    "quota",
    "limit reached",
    "limit · resets",
    "resets at",
)
_CREDENTIAL_MARKERS = (
    "login expired",
    "not logged in",
    "please run /login",
    "please re-run /login",
    "session expired",
    "oauth token has expired",
    "oauth access token has expired",
    "failed to authenticate",
    "authentication failed",
    "invalid api key",
    "invalid authentication credentials",
    "401",
)

_REMEDY = {
    CAUSE_QUOTA: (
        "the account this agent runs on is at its cap. A RESTART DOES NOT FIX "
        "THIS and would destroy the agent's context for nothing — wait for the "
        "reset the message states, or move the agent to an account with "
        "headroom (`sac accounts status` shows 5h/7d usage)"
    ),
    CAUSE_CREDENTIALS: (
        "this agent's credentials are rejected. Re-authenticate the account "
        "(`sac accounts login <account>`, or `/login` in its pane), then `sac "
        "agents restart <name>` — a stale in-memory token only clears on a "
        "restart"
    ),
    CAUSE_UNCLASSIFIED: (
        "the provider refused the turn but the reason is not one this detector "
        "classifies. Read the quoted message, then `sac agents tail <name>` for "
        "the surrounding turns"
    ),
}


@dataclass(frozen=True)
class RefusalRead:
    """What the agent's last turn established about its ability to ACT.

    ``state`` is one of :data:`STATE_REFUSED` / :data:`STATE_CLEAN` /
    :data:`STATE_UNKNOWN` — three-valued, with UNKNOWN a first-class answer
    rather than a pole. ``detail`` always says what was read and why the state
    is what it is, so a caller never has to invent an explanation.

    The validator enforces the two invariants that matter: the state is one of
    the three, and a REFUSED read always names a cause (an unactionable refusal
    is still required to say WHICH failure it was).
    """

    state: str
    detail: str
    cause: str = ""
    text: str = ""
    remedy: str = ""
    at: float | None = None
    transcript: str = ""

    def __post_init__(self) -> None:
        if self.state not in (STATE_REFUSED, STATE_CLEAN, STATE_UNKNOWN):
            raise ValueError(
                f"RefusalRead.state must be one of {STATE_REFUSED!r} / "
                f"{STATE_CLEAN!r} / {STATE_UNKNOWN!r}, got {self.state!r}. "
                f"'I could not tell' is a first-class answer and must be "
                f"spelled {STATE_UNKNOWN!r}, never collapsed into a pole."
            )
        if self.state == STATE_REFUSED and not self.cause:
            raise ValueError(
                f"a RefusalRead in state {STATE_REFUSED!r} must name a cause — "
                f"an unactionable refusal is still required to say WHICH "
                f"failure it was (use {CAUSE_UNCLASSIFIED!r} when it cannot be "
                f"classified)."
            )


def classify_refusal(text: str) -> tuple[str, str]:
    """Return ``(cause, remedy)`` for a refusal message. Pure; never raises.

    CREDENTIALS is tested BEFORE quota because its vocabulary is the more
    specific one: ``API Error: 401 ... rate limit`` is an auth rejection that
    happens to mention a limit, whereas a genuine cap message never mentions
    login. Unrecognised text yields :data:`CAUSE_UNCLASSIFIED` — a refusal we
    cannot name is still a refusal, and reporting it as CLEAN would be the exact
    collapse this instrument exists to prevent.
    """
    lowered = (text or "").lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in lowered:
            return CAUSE_CREDENTIALS, _REMEDY[CAUSE_CREDENTIALS]
    for marker in _QUOTA_MARKERS:
        if marker in lowered:
            return CAUSE_QUOTA, _REMEDY[CAUSE_QUOTA]
    return CAUSE_UNCLASSIFIED, _REMEDY[CAUSE_UNCLASSIFIED]


def _message_text(message: dict) -> str:
    """Concatenate the text blocks of a transcript ``message``. Never raises."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _parse_ts(value: Any) -> float | None:
    """Epoch seconds from a transcript ``timestamp``, or ``None``.

    An unparseable stamp is ``None`` (age unknown), never ``now`` — pretending a
    record is fresh is how a stale refusal would be reported as current.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:  # stx-allow: fallback (reason: an unparseable timestamp is an honest UNKNOWN age — fabricating one would let a stale refusal report as current)
        return None


def _last_assistant_record(tail: str) -> dict | None:
    """The LAST assistant record in a transcript tail, or ``None``.

    Walks backwards, so a long tail costs only the records after the answer. A
    truncated leading line (the slice starts mid-record) fails to parse and is
    skipped — this never assumes the slice began on a record boundary.
    """
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            continue
        try:
            record = json.loads(stripped)
        except ValueError:  # stx-allow: fallback (reason: a tail slice can begin mid-record; an unparseable line is skipped, never treated as evidence)
            continue
        if isinstance(record, dict) and record.get("type") == "assistant":
            return record
    return None


def _read_tail(path: Path, tail_bytes: int) -> str | None:
    """Last ``tail_bytes`` of ``path`` as text, or ``None`` if unreadable."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            return handle.read().decode("utf-8", errors="ignore")
    except OSError:  # stx-allow: fallback (reason: an unreadable transcript is UNKNOWN — a CLEAN here would certify health from a file we never opened)
        return None


def find_transcript(config: Any) -> tuple["Path | None", tuple[str, ...]]:
    """Newest ``*.jsonl`` transcript for ``config``, plus the homes searched.

    Returns ``(path_or_None, candidate_homes)``. Candidates come from
    :func:`..runtimes._apptainer_inner_argv_tui._candidate_transcript_homes` —
    the SAME derivation the ``claude -c`` continue gate uses, reused so the
    place a transcript is looked for here and there cannot drift apart.

    THE CANDIDATES ARE A PROMISE, NOT A FACT (operator ruling 2026-08-12). They
    are derived from the spec's binds/overlay, which say where a transcript
    WOULD go — not where this incarnation wrote one. So the homes are returned
    alongside the answer: a miss must be reported as "we looked HERE and found
    nothing", which is UNKNOWN, never health. Measured on scitex-compute-04
    2026-08-12, this resolver located a transcript for 19 of 107 registered
    agents, so the miss is the common case.
    """
    try:
        from ..runtimes._apptainer_inner_argv_tui import _candidate_transcript_homes

        homes = [Path(home) for home in _candidate_transcript_homes(config)]
    except Exception as exc:  # stx-allow: fallback (reason: an unresolvable home list is UNKNOWN with its reason attached — never a fabricated verdict, never a crashed health command)
        return None, (f"<could not derive candidate homes: {exc}>",)

    newest: Path | None = None
    newest_mtime = -1.0
    for home in homes:
        try:
            candidates = list((home / ".claude" / "projects").glob("*/*.jsonl"))
        except OSError:  # stx-allow: fallback (reason: an unreadable candidate dir is skipped so the remaining candidates still get their chance)
            continue
        for candidate in candidates:
            try:
                mtime = candidate.stat().st_mtime
            except OSError:  # stx-allow: fallback (reason: a file racing deletion during the walk must not abort the search)
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = candidate, mtime
    return newest, tuple(str(home) for home in homes)


def _clean_read(path: Path, message: dict, at: float | None) -> RefusalRead:
    """A CLEAN read — the last turn ran. Not proof of life; see :data:`STATE_CLEAN`."""
    return RefusalRead(
        state=STATE_CLEAN,
        detail=(
            f"the most recent assistant turn in {path} ran normally (model "
            f"{message.get('model') or '?'!r}, no {REFUSAL_FLAG}) — no refusal "
            f"is in evidence. Not proof of life: it says the last turn worked, "
            f"not that the next one will"
        ),
        at=at,
        transcript=str(path),
    )


def last_turn_refusal(
    transcript: "Path | str",
    *,
    now: float | None = None,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> RefusalRead:
    """Did ``transcript``'s most recent assistant turn RUN? Never raises.

    Reads the tail, finds the LAST assistant record, and reports:

    * flagged :data:`REFUSAL_FLAG` and FRESH → :data:`STATE_REFUSED`, carrying
      the cause, the provider's own words and the remedy;
    * flagged but older than ``stale_after_s`` → :data:`STATE_UNKNOWN` (the
      agent has not been asked since; idle and unable look identical here);
    * an ordinary turn → :data:`STATE_CLEAN`;
    * unreadable, or no assistant turn in the window → :data:`STATE_UNKNOWN`,
      naming the path.
    """
    path = Path(transcript)
    tail = _read_tail(path, tail_bytes)
    if tail is None:
        return RefusalRead(
            state=STATE_UNKNOWN,
            detail=(
                f"the transcript {path} could not be read, so nothing is known "
                f"about whether this agent's turns are running"
            ),
            transcript=str(path),
        )

    record = _last_assistant_record(tail)
    if record is None:
        return RefusalRead(
            state=STATE_UNKNOWN,
            detail=(
                f"no assistant turn was found in the last {tail_bytes} bytes of "
                f"{path} — the agent has not answered yet, or its turns are "
                f"older than this window. Nothing was observed either way"
            ),
            transcript=str(path),
        )

    at = _parse_ts(record.get("timestamp"))
    raw_message = record.get("message")
    message = raw_message if isinstance(raw_message, dict) else {}
    text = _message_text(message).strip()

    if not record.get(REFUSAL_FLAG):
        return _clean_read(path, message, at)

    reference = time.time() if now is None else now
    if at is not None and (reference - at) > stale_after_s:
        return RefusalRead(
            state=STATE_UNKNOWN,
            detail=(
                f"the most recent turn in {path} was REFUSED ({text!r}) but that "
                f"was {int(reference - at)}s ago, beyond the "
                f"{int(stale_after_s)}s freshness window — the agent has simply "
                f"not been asked since, and idle is indistinguishable from "
                f"unable from here. UNKNOWN, never a stale accusation"
            ),
            at=at,
            transcript=str(path),
        )

    cause, remedy = classify_refusal(text)
    return RefusalRead(
        state=STATE_REFUSED,
        cause=cause,
        text=text,
        remedy=remedy,
        at=at,
        transcript=str(path),
        detail=(
            f"the most recent assistant turn was REFUSED by the provider — it "
            f"answered {text!r} and no turn ran ({REFUSAL_FLAG}, model "
            f"{message.get('model') or '?'!r}). Cause: {cause}. This agent is "
            f"PRESENT but CANNOT ACT. Remedy: {remedy}. Evidence: {path}"
        ),
    )
