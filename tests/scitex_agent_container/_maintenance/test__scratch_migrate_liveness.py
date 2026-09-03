"""Is the agent running — and may the answer be believed from HERE?

The guard in this module exists because a REAL dry-run produced a real false
negative. Running `sac agents scratch-migrate` from inside the
`scitex-agent-container` agent on scitex-compute-04, 2026-09-03:

    runtime/scitex-agent-container/apptainer_pid   3190806
    highest pid visible in that container's /proc     74275
    the plan said                                   "stopped"

— about the agent whose own turn was executing the probe, and it offered to
move its 10.3 GiB `/uvwork` out from under a mounted overlay. The adapter is
right and the pid file is right; the VANTAGE is wrong, and a wrong answer
that authorises a delete is worse than no answer.

So the tests here are about ABSTENTION, not about a cleverer probe: inside a
container the instrument must decline, and on a host it must not decline for
no reason (the positive control — a guard that always fires would pass the
first half of this file while making the verb useless).

``liveness_vantage`` takes its environment as a parameter precisely so both
sides are reachable without a test having to be run twice on two machines.
STX-TQ002 AAA markers; one fact per test (PA-307).
"""

from __future__ import annotations

from scitex_agent_container._maintenance._scratch_migrate_liveness import (
    CONTAINER_MARKER_ENV,
    liveness_vantage,
)

_IN_APPTAINER = {"APPTAINER_CONTAINER": "/x/sac-base.sif"}
_IN_SINGULARITY = {"SINGULARITY_CONTAINER": "/x/sac-base.sif"}


# ---------------------------------------------------------------------------
# A vantage that CAN see host pids — the positive control
# ---------------------------------------------------------------------------


def test_a_bare_host_vantage_is_not_blind() -> None:
    # Arrange — no container marker: this is the host, where the pid probe
    # means what it says. A guard that fired here would refuse every agent
    # forever.
    env: dict = {}
    # Act
    blind = liveness_vantage(env)
    # Assert
    assert blind == ""


def test_an_unrelated_environment_is_not_blind() -> None:
    # Arrange — only the two markers count; other variables say nothing.
    env = {"HOME": "/home/ywatanabe", "APPTAINER_BIND": "/scratch"}
    # Act
    blind = liveness_vantage(env)
    # Assert
    assert blind == ""


def test_an_empty_marker_value_is_not_blind() -> None:
    # Arrange — an exported-but-empty variable is not evidence of a
    # container, and treating it as one would refuse every host run whose
    # shell happened to export the name.
    env = {"APPTAINER_CONTAINER": "   "}
    # Act
    blind = liveness_vantage(env)
    # Assert
    assert blind == ""


# ---------------------------------------------------------------------------
# A vantage that CANNOT — abstain, loudly
# ---------------------------------------------------------------------------


def test_inside_an_apptainer_container_the_vantage_is_blind() -> None:
    # Arrange — the measured case.
    # Act
    blind = liveness_vantage(_IN_APPTAINER)
    # Assert
    assert blind != ""


def test_inside_a_singularity_container_the_vantage_is_blind() -> None:
    # Arrange — the older spelling of the same marker.
    # Act
    blind = liveness_vantage(_IN_SINGULARITY)
    # Assert
    assert blind != ""


def test_the_blind_reason_names_the_pid_namespace() -> None:
    # Arrange — the reason has to explain WHY the pid probe cannot work,
    # not merely that something is off.
    # Act
    blind = liveness_vantage(_IN_APPTAINER)
    # Assert
    assert "PID namespace" in blind


def test_the_blind_reason_names_the_variable_that_gave_it_away() -> None:
    # Arrange
    # Act
    blind = liveness_vantage(_IN_APPTAINER)
    # Assert
    assert "APPTAINER_CONTAINER=/x/sac-base.sif" in blind


def test_the_blind_reason_names_the_fix() -> None:
    # Arrange — the operator's next action must be in the message.
    # Act
    blind = liveness_vantage(_IN_APPTAINER)
    # Assert
    assert "on the HOST" in blind


def test_both_container_markers_are_watched() -> None:
    # Arrange — apptainer sets both spellings; watching one would leave a
    # container that only exports the other reading as a host.
    expected = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")
    # Act
    watched = CONTAINER_MARKER_ENV
    # Assert
    assert watched == expected
