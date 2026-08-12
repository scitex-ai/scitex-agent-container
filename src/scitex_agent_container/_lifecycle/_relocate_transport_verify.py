"""Did what left arrive? Compared per file, against the numbers RECORDED BEFORE THE COPY.

Split from :mod:`_relocate_transport`, which decides what may travel. This half
decides whether it got there, and it is the only statement the transport makes
about success — a pipeline's exit code says two processes exited.

THE BASELINE IS A SNAPSHOT, NOT A SECOND LOOK AT THE SOURCE. ``sent`` is measured
once, before anything moves, and the copy is bounded by that measurement (see
:func:`.._relocate_transport_ssh.snapshot_transcripts`). Re-measuring the source
afterwards and comparing THAT to the target is the race this feature was rewritten
to remove: the source is entitled to change between the two readings, and when it
does, the comparison reports a corruption that never happened.

SHORT AND LARGER ARE DIFFERENT ANSWERS. Measured 2026-08-12, on a real relocation
that aborted 422::

    MISMATCH b68520e1-….jsonl:
      sent   108,412,278 bytes / 58,325 lines
      target 108,423,995 bytes / 58,325 lines

Read the shape: identical LINE counts, 11,717 bytes apart, and the TARGET holds
MORE. No record was appended — the last line got longer. The agent had already
been stopped and the stop had been confirmed; what grew the file was the dying
process's shutdown flush finishing its final line after tmux had let go of the
session. The report called that "fewer bytes or lines than it left with", which
sends the reader hunting for bytes lost in transit that were never lost.

    ``target < sent``   bytes went missing; the copy is suspect.
    ``target > sent``   nothing went missing; the source moved after we measured
                        it, and the target's copy is the more complete one.

Both refuse — a checker that cannot say WHY must stop — but they carry different
codes and name different next moves.

AN UNMEASURED COUNT ON EITHER SIDE IS UNKNOWN, NOT A PASS. "I could not count it"
and "it counted the same" are the two answers this module exists to keep apart.

Pure. No filesystem, no ssh, no clock — observations in, a verdict out, so every
refusal path is a test with real values rather than a second machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Sequence

__all__ = [
    "CODE_ARRIVED",
    "CODE_MISSING_ON_TARGET",
    "CODE_TARGET_LARGER",
    "CODE_TRUNCATED",
    "CODE_UNKNOWN",
    "ArrivalVerdict",
    "TranscriptFile",
    "verify_arrival",
]

#: Every file that left arrived with matching bytes and lines.
CODE_ARRIVED: Final = 200
#: A file that left is absent on the target. The copy did not happen.
CODE_MISSING_ON_TARGET: Final = 404
#: A file arrived with FEWER bytes or lines than the snapshot recorded. Bytes
#: went missing between the two hosts; the copy itself is what to distrust.
CODE_TRUNCATED: Final = 422
#: A file arrived with MORE than the snapshot recorded. Nothing was lost — the
#: source changed after it was measured (or something appended on the target).
#: A conflict between two readings of the world, not a damaged payload, so it
#: gets its own code rather than being reported as a truncation that ran
#: backwards. It shares 409 with :data:`.._relocate_transport.CODE_SOURCE_RUNNING`
#: because the two are the same sentence in different phases — the world moved
#: under the decision — and the plan/arrival codes are separate families anyway
#: (``CODE_READY`` and ``CODE_ARRIVED`` are both 200).
CODE_TARGET_LARGER: Final = 409
#: Something was not observed. Refuses as firmly as a failure, differently.
CODE_UNKNOWN: Final = 503


@dataclass(frozen=True)
class TranscriptFile:
    """One transcript, as MEASURED on one side of the copy.

    ``byte_count`` and ``line_count`` are ``| None`` for NOT MEASURED, which is
    deliberately distinct from ``0``. An empty file and a measurement that did
    not run look identical to a caller that collapses them, and the second must
    refuse where the first may not.

    On the SOURCE side this carries the SNAPSHOT rather than the whole file:
    ``byte_count`` is the offset of the last newline and ``line_count`` the
    number of complete lines at the instant the snapshot was taken. That is the
    contract the copy is bounded by and the number arrival is checked against.
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
class ArrivalVerdict:
    """Whether everything that left arrived intact, with the evidence attached.

    ``arrived`` is three-valued, no ``__bool__``. ``mismatches`` carries one line
    per offending file so a report names WHICH file, BY HOW MUCH, and IN WHICH
    DIRECTION — rather than saying the transfer failed and leaving the reader to
    go and diff two hosts.
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


def _counts(src: TranscriptFile, tgt: TranscriptFile) -> str:
    return (
        f"sent {src.byte_count} bytes / {src.line_count} lines, "
        f"target holds {tgt.byte_count} bytes / {tgt.line_count} lines"
    )


def _short_line(src: TranscriptFile, tgt: TranscriptFile) -> str:
    """The copy is suspect: bytes that left did not land."""
    return (
        f"{src.name}: arrived SHORT — {_counts(src, tgt)} "
        f"(target {tgt.byte_count - src.byte_count:+d} bytes, "
        f"{tgt.line_count - src.line_count:+d} lines). Bytes that were snapshotted "
        "did not land"
    )


def _larger_line(src: TranscriptFile, tgt: TranscriptFile) -> str:
    """Nothing was lost: the source moved after the snapshot was taken."""
    same_lines = (
        " on the SAME line count, so no record was appended — a line that was "
        "already there simply got longer"
        if tgt.line_count == src.line_count
        else ""
    )
    return (
        f"{src.name}: the target holds MORE — {_counts(src, tgt)} "
        f"(target {tgt.byte_count - src.byte_count:+d} bytes, "
        f"{tgt.line_count - src.line_count:+d} lines){same_lines}. Nothing went "
        "missing; the source changed after it was measured"
    )


def verify_arrival(
    *,
    sent: Sequence[TranscriptFile],
    landed: Sequence[TranscriptFile],
) -> ArrivalVerdict:
    """Compare the SNAPSHOT that was sent with what the TARGET now holds, per file.

    ``sent`` is the snapshot recorded on the source before the copy started;
    ``landed`` is measured on the target afterwards. Every file in ``sent`` must
    appear in ``landed`` with both numbers equal. Files present only in ``landed``
    are ignored — the destination is the agent's own projects directory and may
    hold other conversations, which is not evidence of a bad copy.

    An unmeasured count on either side is UNKNOWN, not a pass.
    """
    if not sent:
        return ArrivalVerdict(
            arrived=None,
            code=CODE_UNKNOWN,
            reason="nothing was recorded as sent, so there is nothing to confirm",
            hint=(
                "snapshot the source files before the copy; without a baseline the "
                "target's contents cannot be checked against anything"
            ),
        )

    by_name = {f.name: f for f in landed}
    mismatches: list[str] = []
    verified: list[str] = []
    unmeasured: list[str] = []
    absent: list[str] = []
    short: list[str] = []
    larger: list[str] = []

    for src in sent:
        if not src.measured:
            unmeasured.append(f"{src.name}: not measured on the source")
            continue
        tgt = by_name.get(src.name)
        if tgt is None:
            absent.append(src.name)
            mismatches.append(f"{src.name}: absent on the target")
            continue
        if not tgt.measured:
            unmeasured.append(f"{src.name}: not measured on the target")
            continue
        if tgt.byte_count == src.byte_count and tgt.line_count == src.line_count:
            verified.append(src.name)
            continue
        if tgt.byte_count < src.byte_count or tgt.line_count < src.line_count:
            short.append(src.name)
            mismatches.append(_short_line(src, tgt))
        else:
            larger.append(src.name)
            mismatches.append(_larger_line(src, tgt))

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
        return _refusal(
            mismatches=mismatches,
            verified=verified,
            absent=absent,
            short=short,
            larger=larger,
        )

    return ArrivalVerdict(
        arrived=True,
        code=CODE_ARRIVED,
        reason=(
            f"all {len(verified)} transcript(s) verified on the target against the "
            "byte and line counts snapshotted before the copy"
        ),
        verified=tuple(verified),
    )


def _refusal(
    *,
    mismatches: list[str],
    verified: list[str],
    absent: list[str],
    short: list[str],
    larger: list[str],
) -> ArrivalVerdict:
    """Which refusal this is. The order is worst-first, so a mix reports the worst.

    Only when EVERY mismatch is a target holding more does the verdict say the
    source moved — one short file in the set means bytes went missing, and that
    is the finding worth acting on first.
    """
    if short:
        return ArrivalVerdict(
            arrived=False,
            code=CODE_TRUNCATED,
            reason=(
                f"{len(short)} file(s) arrived SHORT of the snapshot that was sent"
            ),
            mismatches=tuple(mismatches),
            verified=tuple(verified),
            hint=(
                "do NOT continue the relocation. Move the target's partial copy aside "
                "and re-run the transport; a short transcript resumes without error "
                "and simply forgets the end of the conversation"
            ),
        )
    if absent:
        return ArrivalVerdict(
            arrived=False,
            code=CODE_MISSING_ON_TARGET,
            reason="the target's copy does not match what was sent",
            mismatches=tuple(mismatches),
            verified=tuple(verified),
            hint=(
                "do NOT continue the relocation. The copy did not happen at all for "
                "these files, which is a different question from one that happened "
                "badly — check that the copy command ran and re-run the transport"
            ),
        )
    return ArrivalVerdict(
        arrived=False,
        code=CODE_TARGET_LARGER,
        reason=(
            f"the target holds MORE than was sent for {len(larger)} file(s); the "
            "source changed after it was measured"
        ),
        mismatches=tuple(mismatches),
        verified=tuple(verified),
        hint=(
            "do NOT continue the relocation, and do NOT treat the target's copy as "
            "damaged — it holds more than the snapshot, which is the source settling "
            "rather than bytes going missing. Confirm the source has gone quiescent "
            "(SOURCE_STOP now waits for two identical readings of the file before it "
            "reports success), then re-run the transport so the snapshot and the copy "
            "describe the same instant"
        ),
    )
