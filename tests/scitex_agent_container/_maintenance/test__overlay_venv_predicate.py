"""Every way the invalidation must refuse, and the one way it must act.

Mirrors ``src/scitex_agent_container/_maintenance/_overlay_venv_predicate.py``.

The suite is deliberately lopsided: fourteen refusals to one action. That is the
right shape for a rail whose mutation renames a directory tree — the interesting
question is never "does it fire?" but "does it stay its hand when it does not
know?".

``SAFE`` below is the all-questions-answered baseline; each test perturbs
exactly one fact. The UNKNOWN cases (``None``) and the negative cases (``True``
for a hazard) are tested separately on purpose: they arrive by different routes
— a probe that could not run vs a probe that ran and found the hazard — and a
predicate that could not distinguish them would be reporting a guess.

Pure facts in, checks out, so no filesystem, no apptainer, no root and no mocks
(PA-306) — which is exactly what makes the live-mount refusal testable at all.
"""

from __future__ import annotations

from scitex_agent_container._maintenance._overlay_venv_model import (
    ACTION_INVALIDATE,
    ACTION_NONE,
    ACTION_REFUSE,
    CHECK_AGENT_NOT_RUNNING,
    CHECK_NOT_INSIDE_CONTAINER,
    OverlayVenvFacts,
)
from scitex_agent_container._maintenance._overlay_venv_predicate import (
    plan_invalidation,
)

#: Every question answered, image changed, a venv slice present to move, and a
#: populated lower layer to fall back on once it is moved.
SAFE = OverlayVenvFacts(
    sif_identity="sac-base-2026-0810-195145.sif:100:7",
    recorded_identity="sac-base-2026-0731-101010.sif:99:6",
    venv_slice_present=True,
    inside_container=False,
    agent_running=False,
    upper_mounted_here=False,
    base_provides_venv=True,
)


def _plan(**overrides):
    facts = OverlayVenvFacts(**{**vars(SAFE), **overrides})
    return plan_invalidation(agent="a", overlay_root="/o", facts=facts)


def test_a_fully_observed_stale_overlay_invalidates() -> None:
    """The one case that acts. Everything else in this file refuses."""
    # Arrange
    facts = SAFE
    # Act
    plan = plan_invalidation(agent="a", overlay_root="/o", facts=facts)
    # Assert
    assert plan.action == ACTION_INVALIDATE


def test_running_inside_a_container_refuses() -> None:
    """From inside, a rename becomes a whiteout that masks the SIF's own files."""
    # Arrange
    override = {"inside_container": True}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_the_inside_container_refusal_names_the_whiteout_hazard() -> None:
    """The refusal has to teach, or the next person works around it."""
    # Arrange
    override = {"inside_container": True}
    # Act
    plan = _plan(**override)
    # Assert
    assert "WHITEOUT" in " ".join(plan.blocking_reasons())


def test_unobserved_container_membership_refuses() -> None:
    # Arrange
    override = {"inside_container": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_a_running_agent_refuses() -> None:
    """A live agent's container holds the overlay mounted."""
    # Arrange
    override = {"agent_running": True}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_unobserved_agent_liveness_refuses() -> None:
    """The caller that did not measure liveness must not get a mutation."""
    # Arrange
    override = {"agent_running": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_overlay_mounted_in_this_namespace_refuses() -> None:
    # Arrange
    override = {"upper_mounted_here": True}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_unreadable_mount_table_refuses() -> None:
    """'I could not read /proc' is not 'nothing is mounted'."""
    # Arrange
    override = {"upper_mounted_here": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_unobserved_sif_identity_refuses() -> None:
    # Arrange
    override = {"sif_identity": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_unresolvable_image_refuses() -> None:
    """Observed-and-empty is still no answer: comparing against "" is a coin flip."""
    # Arrange
    override = {"sif_identity": ""}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_unreadable_stamp_refuses() -> None:
    """An unreadable stamp is NOT an unstamped overlay — one authorises a move."""
    # Arrange
    override = {"recorded_identity": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_unreadable_upper_layer_refuses() -> None:
    # Arrange
    override = {"venv_slice_present": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_a_never_stamped_overlay_is_stale() -> None:
    """The fleet's day-one state: no stamp anywhere, and every overlay shadowing.

    An empty stamp must NOT be adopted as "matches whatever is mounted now" —
    that would make this rail a no-op on exactly the 2026-08-11 population it
    was built for.
    """
    # Arrange
    override = {"recorded_identity": ""}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_INVALIDATE


def test_a_matching_stamp_does_nothing() -> None:
    # Arrange
    override = {"recorded_identity": SAFE.sif_identity}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_NONE


def test_a_stale_overlay_with_no_venv_slice_does_nothing() -> None:
    """Nothing in the upper to shadow with, so the image already wins."""
    # Arrange
    override = {"venv_slice_present": False}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_NONE


def test_the_safety_checks_precede_the_staleness_checks() -> None:
    """A report read top-down leads with 'may I act', never 'should I act'."""
    # Arrange
    facts = SAFE
    # Act
    plan = plan_invalidation(agent="a", overlay_root="/o", facts=facts)
    # Assert
    assert [c.name for c in plan.checks[:2]] == [
        CHECK_NOT_INSIDE_CONTAINER,
        CHECK_AGENT_NOT_RUNNING,
    ]


def test_every_reason_is_reported_not_just_the_first() -> None:
    """An operator asking 'why not?' needs every answer in one run."""
    # Arrange
    override = {"inside_container": True, "agent_running": True}
    # Act
    plan = _plan(**override)
    # Assert
    assert len(plan.blocking_reasons()) == 2


def test_an_empty_lower_layer_refuses_the_move() -> None:
    """The move only UNHIDES the image's copy; it does not create one.

    With nothing behind the slice, moving it aside leaves the agent with no venv
    at all — this rail's own repair turning a shadowed-but-working container
    into a dead one, which is worse than the bug.
    """
    # Arrange
    override = {"base_provides_venv": False}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_an_unprobed_lower_layer_refuses_the_move() -> None:
    """ "I could not read the image" is not "the image is fine"."""
    # Arrange
    override = {"base_provides_venv": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_REFUSE


def test_the_lower_layer_refusal_says_the_slice_is_the_only_copy() -> None:
    """The hint has to stop someone 'fixing' it by moving the slice by hand."""
    # Arrange
    override = {"base_provides_venv": False}
    # Act
    plan = _plan(**override)
    # Assert
    assert "only copy" in " ".join(plan.blocking_reasons())


def test_a_fresh_overlay_does_not_need_the_lower_layer_probed() -> None:
    """The probe costs an `apptainer exec`, and it is a precondition for MOVING.

    Not consulting a precondition for an action you are not taking is a pass,
    not an unknown — otherwise every ordinary boot would refuse and pay for an
    image probe it had no use for.
    """
    # Arrange
    override = {
        "recorded_identity": SAFE.sif_identity,
        "base_provides_venv": None,
    }
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_NONE


def test_an_overlay_with_no_slice_does_not_need_the_lower_layer_probed() -> None:
    """Same rule from the other direction: nothing to move, nothing to check."""
    # Arrange
    override = {"venv_slice_present": False, "base_provides_venv": None}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.action == ACTION_NONE


def test_staleness_is_reported_honestly_even_when_refusing() -> None:
    """A silent ``stale=False`` would later be misread as 'checked, and fresh'."""
    # Arrange
    override = {"inside_container": True}
    # Act
    plan = _plan(**override)
    # Assert
    assert plan.stale is True
