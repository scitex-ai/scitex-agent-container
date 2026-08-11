"""Move the conversation across two hosts, and PROVE what landed is what left.

:mod:`_relocate_transcript` verifies ONE payload by digest. This is the phase
around it: what is allowed to travel, what to do about a target that already
holds something, and how arrival is confirmed for a SET of files where each one
can fail on its own.

FOUR RULES, EACH FROM A WAY THIS GOES WRONG SILENTLY.

1. THE SOURCE MUST BE STOPPED. A running agent appends to its ``.jsonl`` while it
   is being read, so the copy is a prefix ending mid-line. jsonl has no trailer
   and no length header, so a torn file is not detectably torn — it parses, it
   resumes, and the conversation simply stops early. That is worse than no
   transfer, because no transfer is visible and this is not. An unobserved
   "is it running" is UNKNOWN and refuses just as firmly: the question is
   cheap to ask and the failure is silent.

2. ONLY TRANSCRIPTS TRAVEL — AN ALLOWLIST, NOT A DENYLIST. Just ``*.jsonl`` from
   the project directory. A denylist is a list of the secrets somebody thought
   of; the first credential file named something new travels. The one that
   matters here is ``~/.claude/.credentials.json``, which sits one level ABOVE
   the projects store and so is already outside the transfer root — the
   allowlist refuses it a second time, and :data:`CREDENTIAL_BASENAMES` names it
   so the refusal is greppable and can be asserted by name rather than inferred
   from a suffix rule.

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
   does not have at all is its own outcome, distinct from one that arrived short
   — the first means the copy did not happen, the second means it happened
   badly, and they call for different next moves.

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

__all__ = [
    "CODE_ARRIVED",
    "CODE_MISSING_ON_TARGET",
    "CODE_NOTHING_TO_CARRY",
    "CODE_READY",
    "CODE_SOURCE_RUNNING",
    "CODE_TRUNCATED",
    "CODE_UNKNOWN",
    "CREDENTIAL_BASENAMES",
    "TRANSCRIPT_SUFFIX",
    "ArrivalVerdict",
    "MoveAside",
    "TranscriptFile",
    "TransportPlan",
    "is_transferable",
    "plan_transport",
    "refusal_for",
    "select_transferable",
    "verify_arrival",
]

#: The ONLY suffix that travels. An allowlist — see rule 2 in the module docstring.
TRANSCRIPT_SUFFIX: Final = ".jsonl"

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
#: Every file that left arrived with matching bytes and lines.
CODE_ARRIVED: Final = 200
#: A file that left is absent on the target. The copy did not happen.
CODE_MISSING_ON_TARGET: Final = 404
#: A file arrived with fewer bytes or lines than it left with.
CODE_TRUNCATED: Final = 422
#: Something was not observed. Refuses as firmly as a failure, differently.
CODE_UNKNOWN: Final = 503


def is_transferable(name: str) -> bool:
    """True only for a conversation transcript. Everything else stays put."""
    base = name.rsplit("/", 1)[-1]
    if base in CREDENTIAL_BASENAMES:
        return False
    return base.endswith(TRANSCRIPT_SUFFIX) and len(base) > len(TRANSCRIPT_SUFFIX)


def refusal_for(name: str) -> str:
    """Why ``name`` is not travelling, in words a report can print."""
    base = name.rsplit("/", 1)[-1]
    if base in CREDENTIAL_BASENAMES:
        return (
            "a credential is never carried — the target re-issues its own; copying "
            "one would leave two hosts holding one identity's secret"
        )
    return (
        f"only conversation transcripts ({TRANSCRIPT_SUFFIX}) travel; this is not one"
    )


@dataclass(frozen=True)
class TranscriptFile:
    """One transcript, as MEASURED on one side of the copy.

    ``byte_count`` and ``line_count`` are ``| None`` for NOT MEASURED, which is
    deliberately distinct from ``0``. An empty file and a measurement that did
    not run look identical to a caller that collapses them, and the second must
    refuse where the first may not.
    """

    name: str
    byte_count: int | None = None
    line_count: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("TranscriptFile.name must be non-empty")

    @property
    def measured(self) -> bool:
        return self.byte_count is not None and self.line_count is not None


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


@dataclass(frozen=True)
class ArrivalVerdict:
    """Whether everything that left arrived intact, with the evidence attached.

    ``arrived`` is three-valued, no ``__bool__``. ``mismatches`` carries one line
    per offending file so a report names WHICH file and BY HOW MUCH, rather than
    saying the transfer failed and leaving the reader to go and diff two hosts.
    """

    arrived: bool | None
    code: int
    reason: str
    mismatches: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    hint: str = ""

    def __post_init__(self) -> None:
        if self.arrived not in (True, False, None):
            raise ValueError(
                f"ArrivalVerdict.arrived must be True/False/None, got {self.arrived!r}"
            )
        if not self.reason:
            raise ValueError("ArrivalVerdict.reason must be non-empty")
        if self.arrived is True and self.code != CODE_ARRIVED:
            raise ValueError(
                f"ArrivalVerdict: arrived=True must carry CODE_ARRIVED, got {self.code}"
            )
        if self.arrived is True and self.mismatches:
            raise ValueError(
                "ArrivalVerdict: an arrival with mismatches is unrepresentable"
            )
        if self.arrived is not True and not self.hint:
            raise ValueError("ArrivalVerdict: a non-arrival must say what to do next")


def select_transferable(
    names: Sequence[str],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Split ``names`` into what travels and what is refused, with reasons.

    Returns ``(carried, refused)`` where ``refused`` pairs each name with the
    sentence explaining it. The refusals are RETURNED rather than dropped so a
    report can show that a credential was seen and declined — a filter whose
    output is only the survivors cannot be told from one that never ran.
    """
    carried: list[str] = []
    refused: list[tuple[str, str]] = []
    for name in names:
        if is_transferable(name):
            carried.append(name)
        else:
            refused.append((name, refusal_for(name)))
    return tuple(carried), tuple(refused)


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

    carried, refused = select_transferable(source_files)
    if not carried:
        return TransportPlan(
            proceed=False,
            code=CODE_NOTHING_TO_CARRY,
            reason=(
                "the source project directory holds no conversation transcript to move"
            ),
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
            destination=f"{target_dir.rstrip('/')}/.old/{stamp}",
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

    return TransportPlan(
        proceed=True,
        code=CODE_READY,
        reason=(
            f"{len(carried)} transcript(s) to copy into {target_dir}; source is stopped"
        ),
        files=carried,
        refused=refused,
        move_aside=move,
    )


def verify_arrival(
    *,
    sent: Sequence[TranscriptFile],
    landed: Sequence[TranscriptFile],
) -> ArrivalVerdict:
    """Compare what left with what the TARGET now holds, per file.

    ``sent`` is measured on the source, ``landed`` on the target, each carrying
    a byte count and a line count. Every file in ``sent`` must appear in
    ``landed`` with both numbers equal. Files present only in ``landed`` are
    ignored — the destination is the agent's own projects directory and may hold
    other conversations, which is not evidence of a bad copy.

    An unmeasured count on either side is UNKNOWN, not a pass. "I could not
    count it" and "it counted the same" are the two answers this function exists
    to keep apart.
    """
    if not sent:
        return ArrivalVerdict(
            arrived=None,
            code=CODE_UNKNOWN,
            reason="nothing was recorded as sent, so there is nothing to confirm",
            hint=(
                "measure the source files before the copy; without a baseline the "
                "target's contents cannot be checked against anything"
            ),
        )

    by_name = {f.name: f for f in landed}
    mismatches: list[str] = []
    verified: list[str] = []
    unmeasured: list[str] = []

    for src in sent:
        if not src.measured:
            unmeasured.append(f"{src.name}: not measured on the source")
            continue
        tgt = by_name.get(src.name)
        if tgt is None:
            mismatches.append(f"{src.name}: absent on the target")
            continue
        if not tgt.measured:
            unmeasured.append(f"{src.name}: not measured on the target")
            continue
        if tgt.byte_count != src.byte_count or tgt.line_count != src.line_count:
            mismatches.append(
                f"{src.name}: sent {src.byte_count} bytes / {src.line_count} lines, "
                f"target holds {tgt.byte_count} bytes / {tgt.line_count} lines"
            )
            continue
        verified.append(src.name)

    if unmeasured:
        return ArrivalVerdict(
            arrived=None,
            code=CODE_UNKNOWN,
            reason="the copy could not be confirmed: " + "; ".join(unmeasured),
            mismatches=tuple(mismatches),
            verified=tuple(verified),
            hint=(
                "count bytes and lines on BOTH sides and compare again. A copy that "
                "cannot be checked has not succeeded — do not hand over the lease on it"
            ),
        )

    if mismatches:
        absent = all("absent on the target" in m for m in mismatches)
        return ArrivalVerdict(
            arrived=False,
            code=CODE_MISSING_ON_TARGET if absent else CODE_TRUNCATED,
            reason="the target's copy does not match what was sent",
            mismatches=tuple(mismatches),
            verified=tuple(verified),
            hint=(
                "do NOT continue the relocation. Move the target's partial copy aside "
                "and re-run the transport; a short transcript resumes without error "
                "and simply forgets the end of the conversation"
            ),
        )

    return ArrivalVerdict(
        arrived=True,
        code=CODE_ARRIVED,
        reason=(
            f"all {len(verified)} transcript(s) verified on the target by byte and "
            "line count"
        ),
        verified=tuple(verified),
    )
