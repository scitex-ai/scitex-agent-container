"""Move the conversation across two hosts, and PROVE what landed is what left.

:mod:`_relocate_transcript` verifies ONE payload by digest. This is the phase
around it: what is allowed to travel, what to do about a target that already
holds something, and how arrival is confirmed for a SET of files where each one
can fail on its own. The arrival half lives in
:mod:`_relocate_transport_verify` and is re-exported here.

FIVE RULES, EACH FROM A WAY THIS GOES WRONG SILENTLY.

1. THE SOURCE MUST BE STOPPED. A running agent appends to its ``.jsonl`` while it
   is being read, so the copy is a prefix ending mid-line. jsonl has no trailer
   and no length header, so a torn file is not detectably torn — it parses, it
   resumes, and the conversation simply stops early. That is worse than no
   transfer, because no transfer is visible and this is not. An unobserved
   "is it running" is UNKNOWN and refuses just as firmly: the question is
   cheap to ask and the failure is silent.

   STOPPED IS NOT THE SAME INSTANT AS QUIESCENT, which is rule 5.

2. THE ALLOWLIST IS ``*.jsonl`` AND ``memory/`` — AN ALLOWLIST, NOT A DENYLIST.
   A denylist is a list of the secrets somebody thought of; the first credential
   file named something new travels. The one that matters here is
   ``~/.claude/.credentials.json``, which sits one level ABOVE the projects store
   and so is already outside the transfer root — the allowlist refuses it a
   second time, and :data:`CREDENTIAL_BASENAMES` names it so the refusal is
   greppable and can be asserted by name rather than inferred from a suffix rule.

   ``memory/`` WAS ADDED BECAUSE LEAVING IT BEHIND WAS MEASURED. On 2026-08-11 a
   completed relocation logged ``REFUSED memory — only conversation transcripts
   (.jsonl) travel; this is not one`` and landed an agent whose target ``memory/``
   was EMPTY while nine files and 20,014 bytes stayed on the source — two of them
   written that same evening from the operator's own corrections. THE TRANSCRIPT
   IS THE CONVERSATION; MEMORY IS THE RULES DISTILLED FROM IT. Carrying only the
   first moves an agent that can recall what was said and has lost every rule it
   wrote for itself.

   It travels WHOLE, by tar, not by the snapshot path: a memory note is not an
   append-only log being flushed, so there is no last newline to cut at.

3. NOTHING IS OVERWRITTEN AND NOTHING IS DELETED. A relocation target may well
   have been this agent's home before, and what is there is the only copy of
   whatever it is. An existing target directory is MOVED ASIDE to
   ``.old/<timestamp>/`` first. Move-aside also makes a retry idempotent: the
   second run finds a clean destination instead of merging into a half-written
   one.

4. ARRIVAL IS CONFIRMED BY CONTENT, PER FILE. ``scp`` exiting 0 says the process
   exited 0. Bytes AND lines are compared for every file, because the two catch
   different things: bytes catch a truncated write, lines catch the case where a
   transport rewrote line endings and left the size plausible. A file the target
   does not have at all is its own outcome, distinct from one that arrived short,
   which is distinct again from one that arrived LARGER — see
   :mod:`_relocate_transport_verify`, which owns that half.

5. WHAT TRAVELS IS A SNAPSHOT TAKEN AT ONE INSTANT, CUT AT THE LAST NEWLINE.
   Before anything moves, each file's byte offset of its last COMPLETE line is
   recorded; exactly that many bytes are carried; and arrival is checked against
   THAT RECORDED NUMBER rather than against a fresh reading of a source that may
   have moved since. That removes the race instead of narrowing it. Cutting at
   the last newline specifically is what keeps the target's final line whole: an
   arbitrary offset lands mid-record and produces malformed JSON inside an
   otherwise valid JSONL file, which is worse than a shorter file. Losing at most
   one partially-written record is the accepted trade.

AN EXTRA FILE ON THE TARGET IS NOT A FAILURE. The destination is the agent's own
projects directory and may legitimately hold other conversations. Every file that
LEFT must have ARRIVED intact; nothing is claimed about files that were already
there, and treating them as corruption would refuse a healthy relocation.

Pure. No filesystem, no ssh, no clock — observations in, a verdict out, so every
refusal path is a test with real values rather than a second machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Sequence

from ._relocate_move_aside import move_aside_destination
from ._relocate_transport_verify import (
    CODE_ARRIVED,
    CODE_MISSING_ON_TARGET,
    CODE_TARGET_LARGER,
    CODE_TRUNCATED,
    CODE_UNKNOWN,
    ArrivalVerdict,
    TranscriptFile,
    verify_arrival,
)

__all__ = [
    "CODE_ARRIVED",
    "CODE_MISSING_ON_TARGET",
    "CODE_NOTHING_TO_CARRY",
    "CODE_READY",
    "CODE_SOURCE_RUNNING",
    "CODE_TARGET_LARGER",
    "CODE_TRUNCATED",
    "CODE_UNKNOWN",
    "CREDENTIAL_BASENAMES",
    "MEMORY_DIRNAME",
    "TRANSCRIPT_SUFFIX",
    "ArrivalVerdict",
    "MoveAside",
    "TranscriptFile",
    "TransportPlan",
    "is_carried_directory",
    "is_transferable",
    "move_aside_destination",
    "plan_transport",
    "refusal_for",
    "select_transferable",
    "verify_arrival",
]

#: The ONLY suffix that travels as a transcript. See rule 2 in the module docstring.
TRANSCRIPT_SUFFIX: Final = ".jsonl"

#: The one DIRECTORY on the allowlist. The rules an agent distilled from its
#: conversations, which a relocation carrying only the conversation leaves behind
#: — measured 2026-08-11, nine files and 20,014 bytes stranded on the source.
MEMORY_DIRNAME: Final = "memory"

#: Credential files, named so the exclusion can be asserted BY NAME. These live
#: at ``~/.claude/.credentials.json``, above the projects store, so they are
#: already outside the transfer root; naming them makes the second refusal
#: explicit rather than an inference from the suffix rule. A credential is
#: re-issued on the target, never carried — a copied token means two hosts
#: holding one identity's secret, and the source's copy outliving the source.
CREDENTIAL_BASENAMES: Final = frozenset(
    {".credentials.json", "credentials.json", ".credentials.json.bak"}
)

#: Everything that must travel was selected and the source is quiesced.
CODE_READY: Final = 200
#: There is no transcript to move. A decided no, not a failure.
CODE_NOTHING_TO_CARRY: Final = 204
#: The source agent is still running; copying now yields a torn transcript.
CODE_SOURCE_RUNNING: Final = 409


def is_transferable(name: str) -> bool:
    """True only for a conversation transcript. Everything else stays put."""
    base = name.rsplit("/", 1)[-1]
    if base in CREDENTIAL_BASENAMES:
        return False
    return base.endswith(TRANSCRIPT_SUFFIX) and len(base) > len(TRANSCRIPT_SUFFIX)


def is_carried_directory(name: str) -> bool:
    """True for the one directory on the allowlist. Travels whole, never snapshotted."""
    return name.rsplit("/", 1)[-1] == MEMORY_DIRNAME


def refusal_for(name: str) -> str:
    """Why ``name`` is not travelling, in words a report can print."""
    base = name.rsplit("/", 1)[-1]
    if base in CREDENTIAL_BASENAMES:
        return (
            "a credential is never carried — the target re-issues its own; copying "
            "one would leave two hosts holding one identity's secret"
        )
    return (
        f"the allowlist carries conversation transcripts ({TRANSCRIPT_SUFFIX}) and "
        f"{MEMORY_DIRNAME}/; this is neither. It stays on the source, which is never "
        "deleted, so it remains there to be inspected or fetched by hand"
    )


@dataclass(frozen=True)
class MoveAside:
    """Whether something is already at the destination, and where it goes.

    ``required`` is three-valued. ``None`` means nobody looked — and proceeding
    on an unchecked destination is how the only copy of an earlier conversation
    gets overwritten, so it refuses rather than assuming an empty target.
    """

    required: bool | None
    destination: str | None
    reason: str

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("MoveAside.reason must be non-empty")
        if self.required is True and not self.destination:
            raise ValueError(
                "MoveAside: a required move must name where the existing directory goes"
            )


@dataclass(frozen=True)
class TransportPlan:
    """What will be copied, where the old contents go, or why nothing happens.

    ``proceed`` is three-valued and there is no ``__bool__``: a plan that could
    not be decided must not read as permission, because the next thing that
    happens is a write onto another machine.
    """

    proceed: bool | None
    code: int
    reason: str
    files: tuple[str, ...] = ()
    #: Whole directories on the allowlist (``memory/``). Carried by tar, never
    #: snapshotted — see :func:`select_transferable`.
    directories: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()
    move_aside: MoveAside | None = None
    hint: str = ""

    def __post_init__(self) -> None:
        if self.proceed not in (True, False, None):
            raise ValueError(
                f"TransportPlan.proceed must be True/False/None, got {self.proceed!r}"
            )
        if not self.reason:
            raise ValueError("TransportPlan.reason must be non-empty")
        if self.proceed is True and self.code != CODE_READY:
            raise ValueError(
                f"TransportPlan: proceed=True must carry CODE_READY, got {self.code}"
            )
        if self.proceed is True and not self.files:
            raise ValueError(
                "TransportPlan: a plan that proceeds must name at least one file — "
                "a transport that copies nothing and reports success is the failure "
                "shape this whole feature exists to prevent"
            )
        if self.proceed is not True and not self.hint:
            raise ValueError(
                "TransportPlan: a plan that does not proceed must say what to do next"
            )


def select_transferable(
    names: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Split ``names`` into transcripts, directories, and what is refused.

    Returns ``(transcripts, directories, refused)``. The two carried kinds are
    kept APART rather than merged into one list because they are carried by
    different means and for different reasons: a transcript travels up to a
    recorded byte offset, a directory travels whole. A caller holding one list
    would have to re-derive which is which, and the first one to get that wrong
    would either snapshot a directory or carry a live log unbounded.

    ``refused`` pairs each remaining name with the sentence explaining it. The
    refusals are RETURNED rather than dropped so a report — and now the
    provenance record on the target — can show that something was seen and
    declined. A filter whose output is only the survivors cannot be told from one
    that never ran.
    """
    transcripts: list[str] = []
    directories: list[str] = []
    refused: list[tuple[str, str]] = []
    for name in names:
        if is_transferable(name):
            transcripts.append(name)
        elif is_carried_directory(name):
            directories.append(name)
        else:
            refused.append((name, refusal_for(name)))
    return tuple(transcripts), tuple(directories), tuple(refused)


def plan_transport(
    *,
    source_running: bool | None,
    source_files: Sequence[str] | None,
    target_dir_exists: bool | None,
    target_dir: str | None,
    stamp: str,
) -> TransportPlan:
    """Decide whether the copy may run, and what it moves aside first.

    ``source_running`` is the OBSERVED state of the source agent's process.
    ``source_files`` is what is in the source's project directory (``None`` =
    not listed). ``target_dir`` is the destination derived from the TARGET's
    workdir — see :mod:`_relocate_transport_paths`; passing the source's path
    here is the mistake that module exists to prevent, so an absent one refuses.
    ``stamp`` names the move-aside directory and must be supplied by the caller
    (this module owns no clock).

    The checks are ordered cheapest-and-most-dangerous first: a running source
    is refused before anything is listed, because that refusal holds regardless
    of what the listing would have said.
    """
    if source_running is None:
        return TransportPlan(
            proceed=None,
            code=CODE_UNKNOWN,
            reason="whether the source agent is still running was not observed",
            hint=(
                "ask the source host before copying. A live agent appends to its "
                "transcript mid-read, and a jsonl truncated mid-line still parses "
                "and still resumes — the conversation just stops early, with nothing "
                "anywhere reporting a problem"
            ),
        )
    if source_running:
        return TransportPlan(
            proceed=False,
            code=CODE_SOURCE_RUNNING,
            reason=(
                "the source agent is still running; copying now would read a "
                "transcript that is being appended to"
            ),
            hint=(
                "stop the source and verify it stopped, then re-run. The resulting "
                "torn transcript would not look torn: it parses, it resumes, and the "
                "conversation simply ends early"
            ),
        )

    if not target_dir:
        return TransportPlan(
            proceed=None,
            code=CODE_UNKNOWN,
            reason="the target-side destination directory was not derived",
            hint=(
                "derive it from the TARGET's resolved workdir "
                "(_relocate_transport_paths.derive_target_dir). A transcript under "
                "the SOURCE's encoded directory name is present, intact and invisible "
                "to the target's runner"
            ),
        )

    if source_files is None:
        return TransportPlan(
            proceed=None,
            code=CODE_UNKNOWN,
            reason="the source's transcript directory was not listed",
            hint=(
                "list the source project directory before copying; an unreadable "
                "listing is not an empty one, and treating it as empty relocates an "
                "agent with no memory"
            ),
        )

    carried, directories, refused = select_transferable(source_files)
    if not carried:
        return TransportPlan(
            proceed=False,
            code=CODE_NOTHING_TO_CARRY,
            reason=(
                "the source project directory holds no conversation transcript to move"
            ),
            directories=directories,
            refused=refused,
            hint=(
                "confirm this is right before continuing — the agent will start on "
                "the target with no memory. If it should have had one, the source's "
                "workdir encoding is the first thing to check"
            ),
        )

    if target_dir_exists is None:
        return TransportPlan(
            proceed=None,
            code=CODE_UNKNOWN,
            reason="whether the target already holds a transcript directory was not observed",
            files=carried,
            directories=directories,
            refused=refused,
            hint=(
                "check the destination before writing to it. A relocation target may "
                "have been this agent's home before, and what is there is the only "
                "copy of it"
            ),
        )

    if target_dir_exists:
        move = MoveAside(
            required=True,
            destination=move_aside_destination(target_dir, stamp),
            reason=(
                "the target already holds a transcript directory for this agent; it "
                "is moved aside, never overwritten and never deleted"
            ),
        )
    else:
        move = MoveAside(
            required=False,
            destination=None,
            reason="the destination is empty; nothing to preserve",
        )

    carrying = f"{len(carried)} transcript(s)" + (
        f" and {'/, '.join(directories)}/" if directories else ""
    )
    return TransportPlan(
        proceed=True,
        code=CODE_READY,
        reason=f"{carrying} to copy into {target_dir}; source is stopped",
        files=carried,
        directories=directories,
        refused=refused,
        move_aside=move,
    )


# :func:`verify_arrival`, :class:`ArrivalVerdict`, :class:`TranscriptFile` and the
# arrival codes now live in :mod:`_relocate_transport_verify` and are re-exported
# above. They are the same public surface; the split is by QUESTION — what may
# travel, versus did it arrive — because the second half grew a snapshot baseline
# and a short/larger distinction, and the file has a line budget.
