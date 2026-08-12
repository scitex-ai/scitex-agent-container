"""The arming gate: may the ``to_home_layers`` sweep stand?

The tests that matter most here are the ones about NOT passing. A gate is only
worth having if it fails when it should, and the two ways this one could fail
open are both cheap to write and easy to leave untested:

* comparing an EMPTY population against an empty population, which the
  underlying ``diff_hook_arming`` reports as ``safe`` by design, and
* an agent that could not be measured being quietly dropped so the remaining
  agents compare clean.

STX-NM002: no mocks — real snapshot values and real spec files under tmp_path.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._maintenance._layers_migration_gate import (
    ArmingSnapshot,
    fleet_arming_snapshot,
    gate_arming,
)

_ARMED = {"PreToolUse": {"guard.sh": "user-shared"}}
_ALSO_ARMED = {"PreToolUse": {"other.sh": "per-agent"}}


def _snapshot(**agents) -> ArmingSnapshot:
    return ArmingSnapshot(origins=dict(agents))


# ---------------------------------------------------------------------------
# The floor — the gate's whole reason to exist beyond the raw diff
# ---------------------------------------------------------------------------


def test_an_empty_comparison_is_never_safe() -> None:
    # Arrange — `diff_hook_arming({}, {})` is `safe` on its own, deliberately.
    # A collector that silently found nothing produces exactly this, so the
    # gate's most likely failure mode must not read as its strongest pass.
    empty = ArmingSnapshot()
    # Act
    verdict = gate_arming(empty, empty, expected=0)
    # Assert
    assert verdict.safe is False


def test_an_empty_comparison_fails_the_floor() -> None:
    # Arrange
    empty = ArmingSnapshot()
    # Act
    verdict = gate_arming(empty, empty, expected=0)
    # Assert
    assert verdict.floor_met is False


def test_a_comparison_short_of_the_population_is_not_safe() -> None:
    # Arrange — one agent measured where the sweep touched two.
    before = _snapshot(a=_ARMED)
    after = _snapshot(a=_ARMED)
    # Act
    verdict = gate_arming(before, after, expected=2)
    # Assert
    assert verdict.safe is False


def test_the_floor_failure_names_both_counts() -> None:
    # Arrange — "not verified" is useless without "over how many".
    before = _snapshot(a=_ARMED)
    # Act
    summary = gate_arming(before, _snapshot(a=_ARMED), expected=2).summary()
    # Assert
    assert "1 agent(s) compared, 2 expected" in summary


def test_a_full_identical_comparison_is_safe() -> None:
    # Arrange — the only shape that may proceed.
    before = _snapshot(a=_ARMED, b=_ALSO_ARMED)
    after = _snapshot(a=_ARMED, b=_ALSO_ARMED)
    # Act
    verdict = gate_arming(before, after, expected=2)
    # Assert
    assert verdict.safe is True


def test_the_safe_summary_says_both_sides_were_measured() -> None:
    # Arrange
    before = _snapshot(a=_ARMED)
    # Act
    summary = gate_arming(before, _snapshot(a=_ARMED), expected=1).summary()
    # Assert
    assert "measured both sides" in summary


# ---------------------------------------------------------------------------
# Unmeasurable on either side blocks — an UNKNOWN must never pass
# ---------------------------------------------------------------------------


def test_an_unmeasurable_before_agent_blocks_the_sweep() -> None:
    # Arrange — the agents we CAN see are identical; one we could not read.
    before = ArmingSnapshot(origins={"a": _ARMED}, unmeasurable=("b: ValueError: x",))
    after = _snapshot(a=_ARMED)
    # Act
    verdict = gate_arming(before, after, expected=1)
    # Assert
    assert verdict.safe is False


def test_an_unmeasurable_after_agent_blocks_the_sweep() -> None:
    # Arrange — measurable before, unreadable after: the migration broke it.
    before = _snapshot(a=_ARMED)
    after = ArmingSnapshot(origins={"a": _ARMED}, unmeasurable=("b: ValueError: x",))
    # Act
    verdict = gate_arming(before, after, expected=1)
    # Assert
    assert verdict.safe is False


def test_an_unmeasurable_agent_is_named_in_the_summary() -> None:
    # Arrange
    before = ArmingSnapshot(origins={"a": _ARMED}, unmeasurable=("b: ValueError: x",))
    # Act
    summary = gate_arming(before, _snapshot(a=_ARMED), expected=1).summary()
    # Assert
    assert "UNMEASURABLE before" in summary


# ---------------------------------------------------------------------------
# The diff's own verdicts still count against the gate
# ---------------------------------------------------------------------------


def test_a_lost_hook_is_not_safe() -> None:
    # Arrange — the dangerous direction: a guard stopped being armed.
    before = _snapshot(a=_ARMED)
    after = _snapshot(a={})
    # Act
    verdict = gate_arming(before, after, expected=1)
    # Assert
    assert verdict.safe is False


def test_a_gained_hook_is_not_safe() -> None:
    # Arrange — the promise verified is "identical", not "no worse".
    before = _snapshot(a={})
    after = _snapshot(a=_ARMED)
    # Act
    verdict = gate_arming(before, after, expected=1)
    # Assert
    assert verdict.safe is False


def test_a_reattributed_hook_is_not_safe() -> None:
    # Arrange — same command, different owning layer.
    before = _snapshot(a={"PreToolUse": {"guard.sh": "user-shared"}})
    after = _snapshot(a={"PreToolUse": {"guard.sh": "per-agent"}})
    # Act
    verdict = gate_arming(before, after, expected=1)
    # Assert
    assert verdict.safe is False


def test_the_verdict_dict_carries_every_field_on_every_call() -> None:
    # Arrange — a consumer must never have to guess which key exists today.
    verdict = gate_arming(_snapshot(a=_ARMED), _snapshot(a=_ARMED), expected=1)
    expected_keys = {
        "safe",
        "floor_met",
        "expected",
        "agents_compared",
        "unchanged",
        "lost",
        "gained",
        "reattributed",
        "unmeasured",
        "unexpected",
        "before_unmeasurable",
        "after_unmeasurable",
        "summary",
    }
    # Act
    payload = verdict.to_dict()
    # Assert
    assert set(payload) == expected_keys


# ---------------------------------------------------------------------------
# The collector — an agent it cannot read must not become "no hooks"
# ---------------------------------------------------------------------------


@pytest.fixture
def broken_spec(tmp_path: Path) -> Path:
    # Arrange — a real file that real `load_config` really refuses.
    agent_dir = tmp_path / "broken-agent"
    agent_dir.mkdir()
    spec = agent_dir / "spec.yaml"
    spec.write_text("this: is not a valid agent spec\n")
    yield spec


def test_an_unmeasurable_spec_stays_out_of_the_origins_map(broken_spec: Path) -> None:
    # Arrange — recording it as `{}` would compare EQUAL to another `{}` and
    # be reported UNCHANGED, which is "I could not tell" read as "it is fine".
    # Act
    snapshot = fleet_arming_snapshot([broken_spec])
    # Assert
    assert snapshot.origins == {}


def test_an_unmeasurable_spec_is_named_with_its_reason(broken_spec: Path) -> None:
    # Arrange
    # Act
    snapshot = fleet_arming_snapshot([broken_spec])
    # Assert
    assert snapshot.unmeasurable[0].startswith("broken-agent: ")


def test_an_unmeasurable_spec_leaves_the_measured_count_at_zero(
    broken_spec: Path,
) -> None:
    # Arrange — `measured` is what the CLI compares against the population.
    # Act
    snapshot = fleet_arming_snapshot([broken_spec])
    # Assert
    assert snapshot.measured == 0
