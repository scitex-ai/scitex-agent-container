#!/usr/bin/env python3
"""A transport that reports success having moved a torn transcript is the whole bug.

The sharpest test in this file is the TRUNCATION one, and it is built from REAL
files rather than hand-written numbers: a transcript is written to disk, a short
copy is made of it, and both are measured with the same counting code. That
matters because the failure being guarded against is not arithmetic — it is a
copy that lands, exits 0, and holds fewer lines than it left with. jsonl carries
no trailer, so the short file parses and resumes and simply forgets the end of
the conversation.

The credential test asserts the exclusion BY NAME. The allowlist already refuses
anything that is not ``.jsonl``, so the assertion is technically redundant — and
that is the point: it pins the property the operator asked for, so a future
change that widens the filter has to fail a test that says "credentials" rather
than quietly starting to carry one.

Real values, real files. Nothing is mocked.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_move_aside import (
    move_aside_destination,
)
from scitex_agent_container._lifecycle._relocate_transport import (
    CODE_ARRIVED,
    CODE_MISSING_ON_TARGET,
    CODE_NOTHING_TO_CARRY,
    CODE_READY,
    CODE_SOURCE_RUNNING,
    CODE_TRUNCATED,
    CODE_UNKNOWN,
    CREDENTIAL_BASENAMES,
    ArrivalVerdict,
    TranscriptFile,
    is_transferable,
    plan_transport,
    select_transferable,
    verify_arrival,
)

STAMP = "20260811-204500"
TARGET_DIR = "/home/agent/.claude/projects/-home-ywatanabe-proj-lead"
LINES = [
    '{"type":"user","text":"the conversation that moved it"}',
    '{"type":"assistant","text":"understood"}',
    '{"type":"user","text":"continue on the new host"}',
]


def _measure(path) -> TranscriptFile:
    """Count bytes and lines the way both sides of a real transfer would."""
    data = path.read_bytes()
    return TranscriptFile(
        name=path.name,
        byte_count=len(data),
        line_count=len(data.splitlines()),
    )


def _ready_plan(**over):
    kwargs = {
        "source_running": False,
        "source_files": ["a.jsonl"],
        "target_dir_exists": False,
        "target_dir": TARGET_DIR,
        "stamp": STAMP,
    }
    kwargs.update(over)
    return plan_transport(**kwargs)


# --------------------------------------------------------------------------
# The source must be stopped
# --------------------------------------------------------------------------


def test_a_quiesced_source_with_a_clean_target_is_ready() -> None:
    # Arrange: the positive control. Without it every refusal below could pass
    # because the fixture is broken rather than because the refusal works.
    # Act
    plan = _ready_plan()
    # Assert
    assert plan.proceed is True


def test_the_ready_plan_carries_the_success_code() -> None:
    # Arrange: callers branch on the code, never on prose.
    # Act
    plan = _ready_plan()
    # Assert
    assert plan.code == CODE_READY


def test_a_running_source_is_refused() -> None:
    # Arrange: THE rule. A live agent appends mid-read, and the resulting file
    # is not detectably torn — it parses, resumes, and ends the conversation early.
    # Act
    plan = _ready_plan(source_running=True)
    # Assert
    assert plan.code == CODE_SOURCE_RUNNING


def test_a_running_source_is_a_decided_no_not_an_unknown() -> None:
    # Arrange: we DID observe it running. That is an answer, and it calls for a
    # different action than "go and measure it".
    # Act
    plan = _ready_plan(source_running=True)
    # Assert
    assert plan.proceed is False


def test_an_unobserved_source_state_refuses_as_unknown() -> None:
    # Arrange: nobody asked whether the source was running. Proceeding would
    # guess about the one condition that silently corrupts the payload.
    # Act
    plan = _ready_plan(source_running=None)
    # Assert
    assert plan.proceed is None


def test_a_running_source_is_refused_before_anything_is_listed() -> None:
    # Arrange: the refusal holds regardless of what the listing would say, so it
    # must not depend on the listing having succeeded.
    # Act
    plan = _ready_plan(source_running=True, source_files=None, target_dir_exists=None)
    # Assert
    assert plan.code == CODE_SOURCE_RUNNING


def test_the_running_source_refusal_says_to_stop_it() -> None:
    # Arrange: a refusal whose hint does not name the next action turns a
    # one-command fix into an investigation.
    # Act
    plan = _ready_plan(source_running=True)
    # Assert
    assert "stop the source" in plan.hint.lower()


# --------------------------------------------------------------------------
# Nothing is overwritten
# --------------------------------------------------------------------------


def test_an_existing_target_directory_is_moved_aside() -> None:
    # Arrange: a relocation target may have been this agent's home before, and
    # what is there is the only copy of it.
    # Act
    plan = _ready_plan(target_dir_exists=True)
    # Assert
    assert plan.move_aside.required is True


def test_the_move_aside_destination_is_dot_old_under_the_timestamp() -> None:
    # Arrange: the recovery path is "restore from .old/<ts>/", so the location
    # has to be predictable rather than merely somewhere safe.
    # Act
    plan = _ready_plan(target_dir_exists=True)
    # Assert
    assert plan.move_aside.destination == move_aside_destination(TARGET_DIR, STAMP)


def test_a_move_aside_still_lets_the_transport_proceed() -> None:
    # Arrange: an occupied destination is a thing to handle, not a reason to
    # refuse — otherwise a second relocation onto a former home is impossible.
    # Act
    plan = _ready_plan(target_dir_exists=True)
    # Assert
    assert plan.proceed is True


def test_an_empty_destination_needs_no_move_aside() -> None:
    # Arrange: the common case must not manufacture an empty .old/ directory.
    # Act
    plan = _ready_plan(target_dir_exists=False)
    # Assert
    assert plan.move_aside.required is False


def test_an_unchecked_destination_refuses_rather_than_assuming_it_is_empty() -> None:
    # Arrange: assuming empty is how the only copy of an earlier conversation
    # gets overwritten.
    # Act
    plan = _ready_plan(target_dir_exists=None)
    # Assert
    assert plan.proceed is None


def test_a_required_move_aside_must_name_its_destination() -> None:
    # Arrange: the invariant lives in the type — a move with nowhere to go is
    # unrepresentable rather than merely discouraged.
    from scitex_agent_container._lifecycle._relocate_transport import MoveAside

    # Act
    build = lambda: MoveAside(required=True, destination="", reason="x")  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        build()


# --------------------------------------------------------------------------
# Credentials never travel
# --------------------------------------------------------------------------


def test_the_credentials_file_is_not_transferable() -> None:
    # Arrange: the operator's explicit requirement, pinned by NAME so a future
    # widening of the filter has to argue with a test that says "credentials".
    # Act
    transferable = is_transferable(".credentials.json")
    # Assert
    assert transferable is False


def test_every_named_credential_basename_is_refused() -> None:
    # Arrange: the constant exists to be exhaustive, so assert over all of it
    # rather than over the one example that happens to be on our minds.
    # Act
    allowed = [n for n in CREDENTIAL_BASENAMES if is_transferable(n)]
    # Assert
    assert allowed == []


def test_a_credential_beside_a_transcript_is_dropped_from_the_selection() -> None:
    # Arrange: the realistic shape — a directory listing containing both.
    # Act
    carried, _ = select_transferable([".credentials.json", "abc.jsonl"])
    # Assert
    assert carried == ("abc.jsonl",)


def test_a_refused_credential_is_reported_rather_than_silently_dropped() -> None:
    # Arrange: a filter whose output is only the survivors cannot be told from
    # one that never ran.
    # Act
    _, refused = select_transferable([".credentials.json", "abc.jsonl"])
    # Assert
    assert refused[0][0] == ".credentials.json"


def test_the_credential_refusal_explains_that_the_target_re_issues_its_own() -> None:
    # Arrange: the reason matters — someone will eventually ask why their agent
    # lost its login, and the answer must already be in the output.
    # Act
    _, refused = select_transferable([".credentials.json"])
    # Assert
    assert "re-issues its own" in refused[0][1]


def test_a_non_transcript_file_does_not_travel() -> None:
    # Arrange: the allowlist, stated positively.
    # Act
    carried, _ = select_transferable(["notes.md", "config.json", "x.jsonl"])
    # Assert
    assert carried == ("x.jsonl",)


def test_a_bare_suffix_is_not_a_transcript() -> None:
    # Arrange: ".jsonl" with no stem is not a session file; accepting it would
    # let an oddly-named artefact through the one filter that guards this.
    # Act
    transferable = is_transferable(".jsonl")
    # Assert
    assert transferable is False


def test_a_directory_holding_no_transcript_is_a_decided_no() -> None:
    # Arrange: distinct from an unreadable listing — we looked, and there is
    # nothing. The agent will start with no memory, which must be said out loud.
    # Act
    plan = _ready_plan(source_files=[".credentials.json", "notes.md"])
    # Assert
    assert plan.code == CODE_NOTHING_TO_CARRY


def test_an_unlisted_source_directory_is_unknown_not_empty() -> None:
    # Arrange: treating a failed listing as "no transcripts" relocates an agent
    # with no memory and reports success doing it.
    # Act
    plan = _ready_plan(source_files=None)
    # Assert
    assert plan.code == CODE_UNKNOWN


def test_a_plan_that_proceeds_must_name_at_least_one_file() -> None:
    # Arrange: a transport that copies nothing and reports success is exactly
    # the failure shape this feature exists to prevent, so the type refuses it.
    from scitex_agent_container._lifecycle._relocate_transport import TransportPlan

    # Act
    build = lambda: TransportPlan(  # noqa: E731
        proceed=True, code=CODE_READY, reason="x", files=()
    )
    # Assert
    with pytest.raises(ValueError):
        build()


def test_a_missing_target_dir_refuses_rather_than_falling_back_to_the_source_path() -> (
    None
):
    # Arrange: the whole point of deriving the destination from the TARGET. A
    # source-shaped path here would produce an invisible transcript.
    # Act
    plan = _ready_plan(target_dir="")
    # Assert
    assert plan.proceed is None


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


def test_neither_outcome_defines_a_bool() -> None:
    # Arrange: `if plan:` / `if verdict:` on an UNKNOWN would read as a yes, and
    # the next thing that happens is a write onto another machine. Python falls
    # back to truthy for any object, so the guard is that we never DEFINE
    # __bool__ — pinned so a future convenience has to argue with a test.
    from scitex_agent_container._lifecycle._relocate_transport import TransportPlan

    # Act
    defined = "__bool__" in vars(TransportPlan) or "__bool__" in vars(ArrivalVerdict)
    # Assert
    assert defined is False


def test_the_move_aside_destination_is_not_inside_the_directory_it_moves() -> None:
    # Arrange: THE bug, measured 2026-08-11 on the canary run's idempotency pass.
    # The destination was computed inside the directory being moved, so `mv`
    # refused with "cannot move a directory into itself" and a clean retry was a
    # dead end. The string was well-formed, so no pure test could have caught it —
    # this one is written from the real `mv` that did.
    plan = plan_transport(
        source_running=False,
        source_files=["a.jsonl"],
        target_dir_exists=True,
        target_dir=TARGET_DIR,
        stamp=STAMP,
    )
    # Act
    destination = plan.move_aside.destination
    # Assert
    assert not destination.startswith(TARGET_DIR.rstrip("/") + "/")


def test_the_move_aside_destination_keeps_the_directory_name() -> None:
    # Arrange: a restore is "move it back", so the displaced copy has to be
    # recognisable as the thing it was rather than a bare timestamp.
    # Act
    destination = move_aside_destination("/a/b/-home-proj", "S")
    # Assert
    assert destination == "/a/b/.old/S/-home-proj"


def test_a_path_with_no_parent_cannot_be_moved_aside() -> None:
    # Arrange: there is nowhere beside it to move it to, and inventing somewhere
    # would put the only copy of a conversation where nobody would look.
    call = lambda: move_aside_destination("/", "S")
    # Act
    attempt = call
    # Assert
    with pytest.raises(ValueError):
        attempt()
