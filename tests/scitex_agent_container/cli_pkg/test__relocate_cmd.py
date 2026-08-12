#!/usr/bin/env python3
"""`sac agents relocate --dry-run` must refuse on unmeasured facts, not proceed.

The 2026-08-07 hand move rewrote `host:` and produced an agent that started,
reported healthy, and did nothing. The defect was not a wrong answer — it was an
UNMEASURED one treated as fine. So the property under test here is that the
command's default posture is refusal, and that its exit codes let a script tell
"the target failed" from "you asked wrongly" from "this is not built yet".

`declared_from_spec` is tested against real spec shapes rather than mocks: it is
pure dict-in/dict-out, and a spec that is half-written is exactly the case the
operator needs printed rather than rejected.
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from scitex_agent_container._lifecycle._residency import current_host
from scitex_agent_container._state.state_db_relocation import record_residency
from scitex_agent_container.cli_pkg._relocate_cmd import (
    _residency_history,
    EXIT_REFUSED,
    EXIT_RETIRED_UNIMPLEMENTED,
    _required_ports,
    declared_from_spec,
    register,
    relocate,
)

#: Shaped like a real sac spec: binds / image / env live under
#: ``spec.apptainer``, NOT at the top of ``spec``. A fixture that put them at
#: the top would let a wrong-level lookup pass its own test.
FULL_SPEC = {
    "metadata": {"labels": {"groups": ["developer"]}},
    "spec": {
        "runtime": "tui",
        "host": "ywata-note-win",
        "workdir": "/home/ywatanabe/proj/thing",
        "a2a": {"port": 19013},  # stx-allow: STX-NL001
        "apptainer": {
            "image": "sac-base.sif",
            "binds": ["/mnt/c:/mnt/c", "/home/ywatanabe/proj"],
            "env": {"SCITEX_CARDS_DB": "postgresql://localhost:5432/cards"},
        },
    },
}


def test_declared_reads_the_workdir() -> None:
    # Arrange: spec.workdir is the container's --pwd and the only checkout key
    # there is; preflight now asks whether it exists on the TARGET.
    # Act
    declared = declared_from_spec(FULL_SPEC)
    # Assert
    assert declared["workdir"] == "/home/ywatanabe/proj/thing"


def test_declared_reads_the_groups_from_metadata_labels() -> None:
    # Arrange: NOT spec.lineage.group, which is the isolation bucket wearing a
    # confusingly similar name.
    # Act
    declared = declared_from_spec(FULL_SPEC)
    # Assert
    assert declared["groups"] == ("developer",)


def test_declared_reads_the_runtime() -> None:
    # Arrange: runtime is the field whose mismatch stopped the 08-07 move
    # (the nas's sac 0.21.9 rejected 'tui').
    # Act
    declared = declared_from_spec(FULL_SPEC)
    # Assert
    assert declared["runtime"] == "tui"


def test_declared_splits_bind_sources_from_their_targets() -> None:
    # Arrange: preflight asks whether the SOURCE path exists on the target host,
    # so "/mnt/c:/mnt/c" must contribute "/mnt/c", not the whole pair.
    # Act
    declared = declared_from_spec(FULL_SPEC)
    # Assert
    assert declared["bind sources"] == ("/mnt/c", "/home/ywatanabe/proj")


def test_declared_reads_the_card_store_from_env() -> None:
    # Arrange: 5432 here, 5442 there — an agent that cannot reach its board
    # runs and records nothing.
    # Act
    declared = declared_from_spec(FULL_SPEC)
    # Assert
    assert declared["card store"] == "postgresql://localhost:5432/cards"


def test_declared_finds_binds_under_apptainer_not_at_the_top() -> None:
    # Arrange: measured 2026-08-08 — a top-level `binds` lookup reported
    # "(none)" for a spec carrying nineteen of them, and "no binds" is exactly
    # the answer that makes the /mnt/c check look satisfied when it never ran.
    top_level_only = {"spec": {"binds": ["/should/not/be/read"]}}
    # Act
    declared = declared_from_spec(top_level_only)
    # Assert
    assert declared["bind sources"] == ()


def test_declared_reads_the_image_from_apptainer() -> None:
    # Arrange: a missing image fails at boot — AFTER the lease has moved.
    # Act
    declared = declared_from_spec(FULL_SPEC)
    # Assert
    assert declared["image"] == "sac-base.sif"


def test_a_pinned_port_becomes_a_required_port() -> None:
    # Arrange: a spec that names a port is asserting it must be free there.
    # Act
    ports = _required_ports(declared_from_spec(FULL_SPEC))
    # Assert
    assert ports == (19013,)  # stx-allow: STX-NL001


def test_auto_is_not_a_required_port() -> None:
    # Arrange: `a2a.port: auto` is a DEFERRAL, not a requirement — sac picks a
    # free port at boot. Coercing it would invent a requirement the spec never
    # made and then fail the move on a clash that cannot happen.
    spec = {"spec": {"a2a": {"port": "auto"}}}
    # Act
    ports = _required_ports(declared_from_spec(spec))
    # Assert
    assert ports == ()


def test_a_numeric_string_port_is_still_a_requirement() -> None:
    # Arrange: yaml gives back "19013" for a quoted port; it pins just as hard.
    spec = {"spec": {"a2a": {"port": "19013"}}}
    # Act
    ports = _required_ports(declared_from_spec(spec))
    # Assert
    assert ports == (19013,)  # stx-allow: STX-NL001


def test_a_half_written_spec_still_yields_a_report() -> None:
    # Arrange: refusing to print because a field is absent would hide exactly
    # what the operator needs to see.
    # Act
    declared = declared_from_spec({"spec": {"runtime": "apptainer"}})
    # Assert
    assert declared["image"] is None


def test_the_host_is_not_a_declared_field() -> None:
    # Arrange: where an agent runs is an OBSERVATION (operator, 2026-08-11).
    # Printing it under "DECLARED (from the spec — not verified by this run)"
    # is the same collapse that makes `sac agents list` report a running agent
    # as `defined`, so it comes from the state db and appears under OBSERVED.
    spec = {"spec": {"host": "ywata-note-win", "runtime": "apptainer"}}
    # Act
    declared = declared_from_spec(spec)
    # Assert
    assert "host" not in declared


def test_a_spec_without_the_outer_key_is_read_directly() -> None:
    # Arrange: some specs nest under "spec:", some do not. Both are real.
    # Act
    declared = declared_from_spec({"runtime": "apptainer"})
    # Assert
    assert declared["runtime"] == "apptainer"


def test_missing_to_is_a_usage_error_not_a_refusal() -> None:
    # Arrange: click's own exit 2 must stay distinguishable from EXIT_REFUSED,
    # so a script can tell "you asked wrongly" from "the target is not ready".
    runner = CliRunner()
    # Act
    result = runner.invoke(relocate, ["scitex-dev"])
    # Assert
    assert result.exit_code == 2


def test_the_exit_codes_are_distinct() -> None:
    # Arrange: four outcomes a caller must be able to branch on — usage (2),
    # target not ready (3), a phase refused (5), a phase could not be measured
    # (6). Collapsing any two is how a script decides "it worked" from a failure.
    # 4 is retired rather than reused: a script written against its old meaning
    # ("not built yet") must not silently start reading a new one.
    from scitex_agent_container.cli_pkg._relocate_run import (
        EXIT_INCOMPLETE,
        EXIT_UNMEASURED,
    )

    # Act
    codes = {2, EXIT_REFUSED, EXIT_INCOMPLETE, EXIT_UNMEASURED}
    # Assert
    assert len(codes) == 4


def test_the_retired_exit_code_is_not_reused() -> None:
    # Arrange: 4 meant "the executing path does not exist". It does now, so the
    # number is retired rather than re-pointed — a script written against the old
    # meaning must not silently start reading a new one.
    from scitex_agent_container.cli_pkg._relocate_run import (
        EXIT_INCOMPLETE,
        EXIT_UNMEASURED,
    )

    # Act
    live = (EXIT_INCOMPLETE, EXIT_UNMEASURED)
    # Assert
    assert EXIT_RETIRED_UNIMPLEMENTED not in live


def test_register_attaches_the_verb_to_a_group() -> None:
    # Arrange: the CLI leaf must be a VERB under the `agents` noun group.
    group = click.Group("agents")
    # Act
    register(group)
    # Assert
    assert "relocate" in group.commands


def test_the_help_says_the_agent_moves_not_the_host() -> None:
    # Arrange: the operator noted that "relocation" reads as if the HOST moves.
    runner = CliRunner()
    # Act
    result = runner.invoke(relocate, ["--help"])
    # Assert
    assert "The AGENT moves, not the host." in result.output


def test_dry_run_is_the_default() -> None:
    # Arrange: a verb that touches live agent state must not do so by default
    # while its executing half is unbuilt.
    param = next(p for p in relocate.params if p.name == "dry_run")
    # Act
    default = param.default
    # Assert
    assert default is True


# ---------------------------------------------------------------------------
# the not-yet-built refusal — accurate, and generated rather than hard-coded
# ---------------------------------------------------------------------------


def test_the_refusal_covers_every_phase() -> None:
    # Arrange: the previous refusal named ONE missing piece and went stale the
    # moment that piece was built. Generating it from the phase table means it
    # cannot claim less than the truth — but only if the table is complete.
    from scitex_agent_container._lifecycle._relocate_phases import PHASES, PREFLIGHT
    from scitex_agent_container.cli_pkg._relocate_cmd import _PHASE_READINESS

    # Act
    covered = tuple(phase for phase, _, _ in _PHASE_READINESS)
    # Assert
    assert covered == tuple(p for p in PHASES if p != PREFLIGHT)


def test_the_notice_names_the_transport_adapter_as_built() -> None:
    # Arrange: the operator's question is "what is missing", and answering it
    # requires saying what is NOT. The ssh adapter exists now.
    from scitex_agent_container.cli_pkg._relocate_cmd import _readiness_notice

    # Act
    text = "\n".join(_readiness_notice())
    # Assert
    assert "_relocate_transport_ssh" in text


def test_the_notice_names_the_handshake_gate_it_runs_the_reply_through() -> None:
    # Arrange: the handshake now has BOTH halves — a delivery and a gate. The
    # notice must still name the gate, because "we sent it a message" and "the
    # answer passed a check" are the two things a reader needs told apart.
    from scitex_agent_container.cli_pkg._relocate_cmd import _readiness_notice

    # Act
    text = "\n".join(_readiness_notice())
    # Assert
    assert "_relocate_handshake" in text


def test_the_notice_says_the_reply_is_observed_on_the_source() -> None:
    # Arrange: the property the whole phase exists for. A notice that said only
    # "delivers the brief" would describe the A->B leg, which is the one the
    # 2026-08-11 measurement showed proves nothing.
    from scitex_agent_container.cli_pkg._relocate_cmd import _readiness_notice

    # Act
    text = "\n".join(_readiness_notice())
    # Assert
    assert "ON THE SOURCE" in text


def test_the_notice_no_longer_claims_the_adapters_are_absent() -> None:
    # Arrange: THE stale sentence, one generation on. It said the phase driver
    # had no I/O adapters wired; that is now false for transport, source_stop and
    # done, and a notice that misstates its own state sends the reader to build
    # something that already exists.
    from scitex_agent_container.cli_pkg._relocate_cmd import _readiness_notice

    # Act
    text = "\n".join(_readiness_notice())
    # Assert
    assert "no I/O adapters wired" not in text


def test_the_notice_marks_the_transport_phase_as_running() -> None:
    # Arrange: the readiness table is the thing that must not overclaim OR
    # underclaim; transport now has nothing missing.
    from scitex_agent_container.cli_pkg._relocate_cmd import _PHASE_READINESS

    # Act
    missing = {phase: gap for phase, _, gap in _PHASE_READINESS}
    # Assert
    assert missing["transport"] == "—"


def test_the_three_target_side_phases_all_have_adapters_now() -> None:
    # Arrange: the sentence this file used to assert was "target_standby has no
    # adapter". It has one, and so do handshake and handover — which is the whole
    # point of the change, so the test that pinned the refusal becomes the test
    # that pins its absence. source_drain is deliberately NOT in this set: it
    # still has no way to tell a RUNNING agent to finish its in-flight work, and
    # claiming otherwise would be the drift this generated notice exists to stop.
    from scitex_agent_container.cli_pkg._relocate_cmd import _PHASE_READINESS

    # Act
    gaps = {
        phase: gap
        for phase, _, gap in _PHASE_READINESS
        if gap != "—" and phase in ("target_standby", "handshake", "handover")
    }
    # Assert
    assert gaps == {}, f"target-side phases still refusing: {sorted(gaps)}"


def test_the_notice_names_the_standby_as_starting_without_the_lease() -> None:
    # Arrange: a standby that claimed the lease would not be a standby, and the
    # reversibility of everything before HANDOVER rests on it not doing so.
    from scitex_agent_container.cli_pkg._relocate_cmd import _PHASE_READINESS

    # Act
    built = {phase: text for phase, text, _ in _PHASE_READINESS}
    # Assert
    assert "WITHOUT the lease" in built["target_standby"]


def test_the_notice_says_nothing_is_ever_deleted() -> None:
    # Arrange: the operator's rollback story. A displaced directory goes to
    # .old/<stamp>/ and a rollback is a human moving it back — never this code
    # deciding to remove something.
    from scitex_agent_container.cli_pkg._relocate_cmd import _readiness_notice

    # Act
    text = "\n".join(_readiness_notice())
    # Assert
    assert "Nothing is deleted at any point" in text


# ---------------------------------------------------------------------------
# WHERE IT RUNS NOW — the residency table wins over a never-ended instance row
#
# Measured 2026-08-12 on scitex-compute-04: `agent_residency` said
# canary-resume-test lives on ywata-note-win (written by the relocation that
# moved it there), while an `instances` row on scitex-compute-04 had never been
# ended. Reading the instance row first made the command answer with the host
# the agent had LEFT — and a relocation back was then refused with "already
# recorded on that host — nothing to relocate".
#
# The stopped case makes this the NORMAL path rather than an edge one:
# source_drain requires the agent stopped, a stopped agent has no active
# instance row at all, and the next fallback is the spec's legacy `host:` which
# a relocation never updates.
# ---------------------------------------------------------------------------


def test_the_residency_table_answers_where_the_agent_runs() -> None:
    # Arrange: a relocation's DONE phase wrote this stay.
    record_residency(agent="canary", host="ywata-note-win", now=1_786_490_272.0)
    # Act
    history = _residency_history("canary")
    # Assert
    assert current_host(history) == "ywata-note-win"


def test_a_closed_stay_is_carried_too() -> None:
    # Arrange: two moves, so the history is more than "where is it now".
    record_residency(agent="canary", host="scitex-compute-04", now=1_786_400_000.0)
    record_residency(agent="canary", host="ywata-note-win", now=1_786_490_272.0)
    # Act
    history = _residency_history("canary")
    # Assert
    assert len(history) == 2


def test_the_latest_stay_is_the_open_one() -> None:
    # Arrange
    record_residency(agent="canary", host="scitex-compute-04", now=1_786_400_000.0)
    record_residency(agent="canary", host="ywata-note-win", now=1_786_490_272.0)
    # Act
    history = _residency_history("canary")
    # Assert
    assert current_host(history) == "ywata-note-win"


def test_an_agent_the_table_never_heard_of_yields_no_history() -> None:
    # Arrange: genuinely "the db knows nothing", which is what lets a legacy
    # spec host: seed it ONCE. An invented answer here would defeat that.
    # Act
    history = _residency_history("never-relocated")
    # Assert
    assert history == ()
