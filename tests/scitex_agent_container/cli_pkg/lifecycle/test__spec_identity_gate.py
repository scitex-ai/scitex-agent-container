"""Remote launch requires exact caller/target recipe identity."""

import pytest

from scitex_agent_container.cli_pkg.lifecycle._dispatch import (
    _require_spec_identity,
)
from scitex_agent_container.cli_pkg.lifecycle._spec_handoff import HandoffPlan


def test_identical_manifests_pass() -> None:
    plan = HandoffPlan(new=(), changed=(), extra=())
    _require_spec_identity(plan, name="alpha", peer="compute-03")


@pytest.mark.parametrize(
    "plan",
    [
        HandoffPlan(new=("spec.yaml",), changed=(), extra=()),
        HandoffPlan(new=(), changed=("spec.yaml",), extra=()),
        HandoffPlan(new=(), changed=(), extra=("to_home/AGENTS.md",)),
    ],
)
def test_any_manifest_difference_refuses(plan: HandoffPlan) -> None:
    with pytest.raises(RuntimeError, match="Spec identity mismatch"):
        _require_spec_identity(plan, name="alpha", peer="compute-03")


def test_refusal_names_synchronization_not_bypass() -> None:
    plan = HandoffPlan(new=(), changed=("spec.yaml",), extra=())
    with pytest.raises(RuntimeError) as caught:
        _require_spec_identity(plan, name="alpha", peer="compute-03")
    message = str(caught.value)
    assert "--force` cannot bypass" in message
