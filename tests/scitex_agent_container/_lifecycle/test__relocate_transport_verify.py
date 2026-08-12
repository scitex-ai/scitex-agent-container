#!/usr/bin/env python3
"""A transport that reports success having moved a torn transcript is the whole bug.

The sharpest tests here are built from REAL files rather than hand-written
numbers: a transcript is written to disk, a real short copy is made of it, and
both are measured with the same counting code. That matters because the failure
being guarded against is not arithmetic — it is a copy that lands, exits 0, and
holds fewer lines than it left with. jsonl carries no trailer, so the short file
parses and resumes and simply forgets the end of the conversation.

The 2026-08-12 test is the mirror image and is the reason this module exists
separately. The relocation that failed that night reported a mismatch in which the
TARGET held 11,717 bytes MORE than the source measurement, on an IDENTICAL line
count — a source that finished flushing its last line after being measured, not a
copy that lost anything. It was reported in the words of a truncation. Two of the
tests below assert that those two situations no longer produce the same message,
and one asserts the fix that removes the situation altogether: verification is
against the SNAPSHOT, so a source that grows afterwards still verifies.

Real values, real files. Nothing is mocked.
"""

from __future__ import annotations

import subprocess

import pytest

from scitex_agent_container._lifecycle._relocate_shell import Shell
from scitex_agent_container._lifecycle._relocate_transport_ssh import (
    measure_transcripts,
    snapshot_transcripts,
)
from scitex_agent_container._lifecycle._relocate_transport_verify import (
    CODE_ARRIVED,
    CODE_MISSING_ON_TARGET,
    CODE_TARGET_LARGER,
    CODE_TRUNCATED,
    CODE_UNKNOWN,
    ArrivalVerdict,
    TranscriptFile,
    verify_arrival,
)

LOCAL = Shell(host="here", is_local=True)

LINES = [
    '{"type":"user","text":"the conversation that moved it"}',
    '{"type":"assistant","text":"understood"}',
    '{"type":"user","text":"continue on the new host"}',
]


def _real_exec(argv, timeout_s=None):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    return {
        "exit_code": done.returncode,
        "stdout": done.stdout,
        "stderr": done.stderr,
        "timed_out": False,
    }


def _measure(path) -> TranscriptFile:
    """Count bytes and lines the way both sides of a real transfer would."""
    data = path.read_bytes()
    return TranscriptFile(
        name=path.name,
        byte_count=len(data),
        line_count=len(data.splitlines()),
    )


def _carry(source, destination, byte_offset: int) -> None:
    """Copy exactly ``byte_offset`` bytes, with the real tool the transport uses."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["head", "-c", str(byte_offset), "--", str(source)],
        capture_output=True,
    )
    destination.write_bytes(done.stdout)


# --------------------------------------------------------------------------
# The snapshot removes the race: a source that grows still verifies
# --------------------------------------------------------------------------


def test_a_source_that_grows_between_the_snapshot_and_the_copy_still_verifies(
    tmp_path,
) -> None:
    # Arrange: THE 2026-08-12 fix, end to end with real files and the real
    # tools. The offset is recorded, the source then grows exactly as the dying
    # process's flush made it grow, and the copy is still bounded by the number
    # taken first.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    snapshot = snapshot_transcripts(
        LOCAL, str(src_dir), ["sess.jsonl"], exec_fn=_real_exec
    )
    with open(src, "ab") as handle:
        handle.write(b'{"type":"assistant","text":"a late flush"}\n')
    tgt_dir = tmp_path / "tgt"
    _carry(src, tgt_dir / "sess.jsonl", snapshot[0].byte_count)
    landed = measure_transcripts(
        LOCAL, str(tgt_dir), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Act
    verdict = verify_arrival(sent=snapshot, landed=landed)
    # Assert
    assert verdict.arrived is True


def test_the_growth_does_not_reach_the_target(tmp_path) -> None:
    # Arrange: the other half of the same property. "It verifies" would be
    # worthless if the copy had simply carried the extra bytes too — the bound
    # has to be the recorded number, not whatever was there at read time.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    snapshot = snapshot_transcripts(
        LOCAL, str(src_dir), ["sess.jsonl"], exec_fn=_real_exec
    )
    with open(src, "ab") as handle:
        handle.write(b'{"type":"assistant","text":"a late flush"}\n')
    landed_path = tmp_path / "tgt" / "sess.jsonl"
    # Act
    _carry(src, landed_path, snapshot[0].byte_count)
    # Assert
    assert landed_path.stat().st_size < src.stat().st_size


def test_a_half_written_final_record_does_not_travel(tmp_path) -> None:
    # Arrange: the reason the cut is at the last NEWLINE. Carrying a partial
    # record would hand the target malformed JSON inside a file that still parses
    # as JSONL everywhere else, which is worse than a shorter file.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n" + '{"type":"assis', encoding="utf-8")
    snapshot = snapshot_transcripts(
        LOCAL, str(src_dir), ["sess.jsonl"], exec_fn=_real_exec
    )
    landed_path = tmp_path / "tgt" / "sess.jsonl"
    _carry(src, landed_path, snapshot[0].byte_count)
    # Act
    tail = landed_path.read_bytes()
    # Assert
    assert tail.endswith(b"\n")


def test_the_carried_prefix_verifies_against_its_own_snapshot(tmp_path) -> None:
    # Arrange: the same torn-record source, checked the way the transport checks
    # it. A file cut at the last newline is a complete, verifiable payload.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n" + '{"type":"assis', encoding="utf-8")
    snapshot = snapshot_transcripts(
        LOCAL, str(src_dir), ["sess.jsonl"], exec_fn=_real_exec
    )
    tgt_dir = tmp_path / "tgt"
    _carry(src, tgt_dir / "sess.jsonl", snapshot[0].byte_count)
    landed = measure_transcripts(
        LOCAL, str(tgt_dir), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Act
    verdict = verify_arrival(sent=snapshot, landed=landed)
    # Assert
    assert verdict.arrived is True


# --------------------------------------------------------------------------
# Arrival is confirmed by content — the truncation case, from real files
# --------------------------------------------------------------------------


def test_an_identical_copy_is_confirmed(tmp_path) -> None:
    # Arrange: the positive control for the verification half, built from real
    # files so the counting code is exercised rather than assumed.
    src = tmp_path / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    dst = tmp_path / "landed" / "sess.jsonl"
    dst.parent.mkdir()
    dst.write_bytes(src.read_bytes())
    # Act
    verdict = verify_arrival(sent=[_measure(src)], landed=[_measure(dst)])
    # Assert
    assert verdict.arrived is True


def test_a_confirmed_arrival_carries_the_success_code(tmp_path) -> None:
    # Arrange: same fixture, asserting the code callers branch on.
    src = tmp_path / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    dst = tmp_path / "sess-copy.jsonl"
    dst.write_bytes(src.read_bytes())
    landed = TranscriptFile(
        name="sess.jsonl",
        byte_count=len(dst.read_bytes()),
        line_count=len(dst.read_bytes().splitlines()),
    )
    # Act
    verdict = verify_arrival(sent=[_measure(src)], landed=[landed])
    # Assert
    assert verdict.code == CODE_ARRIVED


def test_a_truncated_copy_is_caught(tmp_path) -> None:
    # Arrange: THE case. A real transcript, and a real short copy of it — the
    # shape a partial transfer leaves behind. It parses; nothing else notices.
    src = tmp_path / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    short = tmp_path / "landed" / "sess.jsonl"
    short.parent.mkdir()
    short.write_text(LINES[0] + "\n", encoding="utf-8")
    # Act
    verdict = verify_arrival(sent=[_measure(src)], landed=[_measure(short)])
    # Assert
    assert verdict.arrived is False


def test_a_truncated_copy_names_the_truncation(tmp_path) -> None:
    # Arrange: distinguishing "arrived short" from "never arrived" is what tells
    # the operator whether to retry the copy or go and find out why it never ran.
    src = tmp_path / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    short = tmp_path / "landed" / "sess.jsonl"
    short.parent.mkdir()
    short.write_text(LINES[0] + "\n", encoding="utf-8")
    # Act
    verdict = verify_arrival(sent=[_measure(src)], landed=[_measure(short)])
    # Assert
    assert verdict.code == CODE_TRUNCATED


def test_the_truncation_report_names_the_file_and_both_counts(tmp_path) -> None:
    # Arrange: "the transfer failed" without the numbers means going and diffing
    # two hosts by hand.
    src = tmp_path / "sess.jsonl"
    src.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    short = tmp_path / "landed" / "sess.jsonl"
    short.parent.mkdir()
    short.write_text(LINES[0] + "\n", encoding="utf-8")
    # Act
    verdict = verify_arrival(sent=[_measure(src)], landed=[_measure(short)])
    # Assert
    assert "sess.jsonl" in verdict.mismatches[0] and "3 lines" in verdict.mismatches[0]


def test_a_same_size_copy_with_a_different_line_count_is_caught() -> None:
    # Arrange: why BOTH counts are compared. A transport that rewrote line
    # endings can leave the byte count plausible while losing record boundaries.
    sent = [TranscriptFile("s.jsonl", byte_count=120, line_count=3)]
    landed = [TranscriptFile("s.jsonl", byte_count=120, line_count=1)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert verdict.arrived is False


# --------------------------------------------------------------------------
# Short and larger are different answers — the 2026-08-12 report
# --------------------------------------------------------------------------


def test_a_target_holding_more_is_not_reported_as_a_truncation() -> None:
    # Arrange: the exact numbers from the 2026-08-12 abort. It fired on MORE and
    # said "fewer bytes or lines than it left with", which sends the reader
    # hunting for bytes lost in transit that were never lost.
    sent = [TranscriptFile("b68520e1.jsonl", byte_count=108412278, line_count=58325)]
    landed = [TranscriptFile("b68520e1.jsonl", byte_count=108423995, line_count=58325)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert verdict.code == CODE_TARGET_LARGER


def test_a_target_holding_more_still_refuses() -> None:
    # Arrange: a checker that cannot say WHY must stop. The remedy differs; the
    # refusal does not.
    sent = [TranscriptFile("s.jsonl", byte_count=100, line_count=3)]
    landed = [TranscriptFile("s.jsonl", byte_count=111, line_count=3)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert verdict.arrived is False


def test_short_and_larger_do_not_produce_the_same_message() -> None:
    # Arrange: THE complaint. The two were reported identically, and they call
    # for opposite next moves — distrust the copy, versus re-measure the source.
    sent = [TranscriptFile("s.jsonl", byte_count=100, line_count=3)]
    # Act
    short = verify_arrival(
        sent=sent, landed=[TranscriptFile("s.jsonl", byte_count=90, line_count=2)]
    )
    larger = verify_arrival(
        sent=sent, landed=[TranscriptFile("s.jsonl", byte_count=111, line_count=3)]
    )
    # Assert
    assert short.mismatches[0] != larger.mismatches[0] and short.hint != larger.hint


def test_the_larger_report_says_nothing_went_missing() -> None:
    # Arrange: the operator's first question on seeing a mismatch is "did I lose
    # part of the conversation". The answer is in the line, not in an inference.
    sent = [TranscriptFile("s.jsonl", byte_count=100, line_count=3)]
    landed = [TranscriptFile("s.jsonl", byte_count=111, line_count=3)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert "Nothing went\nmissing" in verdict.mismatches[0].replace(" ", "\n", 0) or (
        "Nothing went missing" in verdict.mismatches[0]
    )


def test_an_identical_line_count_is_called_out_as_no_record_appended() -> None:
    # Arrange: the diagnostic that identifies this shape. Same line count means
    # a line that was already there simply got longer — a shutdown flush, not a
    # continuing conversation.
    sent = [TranscriptFile("s.jsonl", byte_count=100, line_count=3)]
    landed = [TranscriptFile("s.jsonl", byte_count=111, line_count=3)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert "no record was appended" in verdict.mismatches[0]


def test_a_short_file_among_larger_ones_reports_the_short_one() -> None:
    # Arrange: worst-first. One file missing bytes is the finding worth acting
    # on, and a mixed set must not be softened into "the source moved".
    sent = [
        TranscriptFile("a.jsonl", byte_count=100, line_count=3),
        TranscriptFile("b.jsonl", byte_count=100, line_count=3),
    ]
    landed = [
        TranscriptFile("a.jsonl", byte_count=111, line_count=3),
        TranscriptFile("b.jsonl", byte_count=50, line_count=1),
    ]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert verdict.code == CODE_TRUNCATED


def test_the_larger_hint_says_not_to_treat_the_targets_copy_as_damaged() -> None:
    # Arrange: the remedy is the opposite of the truncation one. Discarding the
    # target's copy here would throw away the MORE complete of the two.
    sent = [TranscriptFile("s.jsonl", byte_count=100, line_count=3)]
    landed = [TranscriptFile("s.jsonl", byte_count=111, line_count=3)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert "damaged" in verdict.hint


# --------------------------------------------------------------------------
# Absent, unmeasured, and the type's own invariants
# --------------------------------------------------------------------------


def test_a_file_absent_on_the_target_is_its_own_outcome() -> None:
    # Arrange: the copy did not happen at all, which is a different next move
    # from a copy that happened badly.
    sent = [TranscriptFile("s.jsonl", byte_count=10, line_count=1)]
    # Act
    verdict = verify_arrival(sent=sent, landed=[])
    # Assert
    assert verdict.code == CODE_MISSING_ON_TARGET


def test_an_unmeasured_target_file_is_unknown_not_a_pass() -> None:
    # Arrange: "I could not count it" and "it counted the same" are the two
    # answers this function exists to keep apart.
    sent = [TranscriptFile("s.jsonl", byte_count=10, line_count=1)]
    landed = [TranscriptFile("s.jsonl", byte_count=None, line_count=None)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert verdict.arrived is None


def test_an_unmeasured_source_file_is_unknown_not_a_pass() -> None:
    # Arrange: the same rule on the other side — without a baseline there is
    # nothing to compare the target against.
    sent = [TranscriptFile("s.jsonl")]
    landed = [TranscriptFile("s.jsonl", byte_count=10, line_count=1)]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert verdict.code == CODE_UNKNOWN


def test_an_empty_sent_set_cannot_confirm_anything() -> None:
    # Arrange: comparing nothing to nothing must not read as a successful
    # transfer — that is a green light produced by the absence of evidence.
    # Act
    verdict = verify_arrival(sent=[], landed=[])
    # Assert
    assert verdict.arrived is None


def test_an_extra_file_on_the_target_is_not_a_failure() -> None:
    # Arrange: the destination is the agent's own projects directory and may
    # legitimately hold other conversations. Refusing here would refuse a
    # healthy relocation onto a host the agent lived on before.
    sent = [TranscriptFile("s.jsonl", byte_count=10, line_count=1)]
    landed = [
        TranscriptFile("s.jsonl", byte_count=10, line_count=1),
        TranscriptFile("older.jsonl", byte_count=999, line_count=42),
    ]
    # Act
    verdict = verify_arrival(sent=sent, landed=landed)
    # Assert
    assert verdict.arrived is True


def test_an_arrival_with_mismatches_is_unrepresentable() -> None:
    # Arrange: a success may not disagree with its own evidence — the invariant
    # lives in the type rather than in the discipline of every call site.
    # Act
    build = lambda: ArrivalVerdict(  # noqa: E731
        arrived=True, code=CODE_ARRIVED, reason="x", mismatches=("s.jsonl: short",)
    )
    # Assert
    with pytest.raises(ValueError):
        build()


def test_the_verdict_defines_no_bool() -> None:
    # Arrange: `if verdict:` on an UNKNOWN would read as a yes, and the next
    # thing that happens is a write onto another machine. Python falls back to
    # truthy for any object, so the guard is that we never DEFINE __bool__ —
    # pinned so a future convenience has to argue with a test.
    # Act
    defined = "__bool__" in vars(ArrivalVerdict)
    # Assert
    assert defined is False
