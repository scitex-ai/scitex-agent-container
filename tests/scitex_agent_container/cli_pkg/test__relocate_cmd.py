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

from scitex_agent_container.cli_pkg._relocate_cmd import (
    EXIT_REFUSED,
    EXIT_UNIMPLEMENTED,
    _required_ports,
    declared_from_spec,
    register,
    relocate,
)

#: Shaped like a real sac spec: binds / image / env live under
#: ``spec.apptainer``, NOT at the top of ``spec``. A fixture that put them at
#: the top would let a wrong-level lookup pass its own test.
FULL_SPEC = {
    "spec": {
        "runtime": "tui",
        "host": "ywata-note-win",
        "a2a": {"port": 19013},  # stx-allow: STX-NL001
        "apptainer": {
            "image": "sac-base.sif",
            "binds": ["/mnt/c:/mnt/c", "/home/ywatanabe/proj"],
            "env": {"SCITEX_CARDS_DB": "postgresql://localhost:5432/cards"},
        },
    }
}


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
    # Arrange: three outcomes a caller must be able to branch on — usage (2),
    # target not ready (3), not built yet (4). Collapsing any two is how a
    # script decides "it worked" from a failure.
    # Act
    codes = {2, EXIT_REFUSED, EXIT_UNIMPLEMENTED}
    # Assert
    assert len(codes) == 3


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
