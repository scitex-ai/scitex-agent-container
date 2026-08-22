"""Fifteen fleet specs bind paths absent on compute-04, and every one is correct.

That is the fact this module was written against (measured 2026-08-11): nine
Spartan agents binding shared cluster storage a workstation cannot provide, and
six laptop agents binding a dataset and a checkout that exist on exactly one
machine because that machine made them. Printed as "path not found" they are
indistinguishable; the operator's action differs completely.

The tests below are written per SHAPE rather than per path, because the shapes
are what the classifier can actually see. The credential case gets the most
attention: a hint that says "copy it with the agent" applied to ``~/.ssh`` is
worse than no hint at all.

Pure path shapes in, category out. No filesystem, no network, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_bind_kind import (
    ACTION_CARRY,
    ACTION_DECIDE,
    ACTION_PROVISION,
    KIND_AGENT_LOCAL,
    KIND_CREDENTIAL,
    KIND_HOST_INFRA,
    KIND_UNCLASSIFIED,
    classify_bind,
    classify_binds,
    group_by_action,
)

WORKDIR = "/home/ywatanabe/proj/paper-scitex-clew"
SPARTAN_WORKDIR = "/data/gpfs/projects/punim0264/ywatanabe/paper-scitex-clew"
SRC = "ywata-note-win"


def _kind(path: str, workdir: str = WORKDIR) -> str:
    return classify_bind(path, workdir=workdir, from_host=SRC).kind


# ---------------------------------------------------------------------------
# credentials — the category that exists to prevent a HARMFUL hint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/home/ywatanabe/.ssh",
        "/home/ywatanabe/.config/gh",
        "/home/ywatanabe/.scitex/agent-container/accounts/x/.credentials.json",
        "/home/ywatanabe/.pgpass",
        "/home/ywatanabe/.gnupg",
    ],
)
def test_key_material_is_recognised_as_a_credential(path: str) -> None:
    # Arrange
    workdir = WORKDIR
    # Act
    kind = _kind(path, workdir)
    # Assert
    assert kind == KIND_CREDENTIAL


def test_a_credential_is_never_told_to_travel() -> None:
    # Arrange: "move it with the agent" applied to ~/.ssh means copying key
    # material between machines, which this fleet forbids outright.
    result = classify_bind("/home/ywatanabe/.ssh", workdir=WORKDIR, from_host=SRC)
    # Act
    action = result.action
    # Assert
    assert action == ACTION_PROVISION


def test_a_credential_hint_says_not_to_copy_it() -> None:
    # Arrange
    result = classify_bind("/home/ywatanabe/.ssh", workdir=WORKDIR, from_host=SRC)
    # Act
    fix = result.fix
    # Assert
    assert "do NOT copy it" in fix


def test_an_account_file_under_the_agents_own_home_is_still_a_credential() -> None:
    # Arrange: it sits under .scitex, which would otherwise read as agent data.
    # Credentials are decided FIRST for exactly this collision.
    path = "/home/ywatanabe/.scitex/agent-container/accounts/y/.credentials.json"
    # Act
    kind = _kind(path)
    # Assert
    assert kind == KIND_CREDENTIAL


# ---------------------------------------------------------------------------
# agent-local — the six laptop agents
# ---------------------------------------------------------------------------


def test_a_dataset_path_must_travel_with_the_agent() -> None:
    # Arrange: the laptop shape — the dataset exists where it was made.
    path = "/home/ywatanabe/proj/paper-scitex-clew/.scitex/dataset/bixbench/capsule-001"
    # Act
    action = classify_bind(path, workdir=WORKDIR, from_host=SRC).action
    # Assert
    assert action == ACTION_CARRY


def test_a_path_under_the_workdir_must_travel() -> None:
    # Arrange: results the agent produced inside its own working directory.
    path = f"{WORKDIR}/runs/cohort_a/capsule-001"
    # Act
    action = classify_bind(path, workdir=WORKDIR, from_host=SRC).action
    # Assert
    assert action == ACTION_CARRY


def test_a_sibling_of_the_workdir_is_agent_local_not_infrastructure() -> None:
    # Arrange: THE Spartan shape — .../ywatanabe/scitex-clew/src sits beside
    # .../ywatanabe/paper-scitex-clew. Under a /data root, so a naive
    # first-component rule would call it a mount and send the operator to the
    # storage team for a directory that is a git checkout.
    path = "/data/gpfs/projects/punim0264/ywatanabe/scitex-clew/src/scitex_clew"
    # Act
    kind = _kind(path, workdir=SPARTAN_WORKDIR)
    # Assert
    assert kind == KIND_AGENT_LOCAL


def test_the_agent_local_hint_mentions_cloning_when_it_might_be_a_checkout() -> None:
    # Arrange: the classifier cannot stat the target, so it names both routes
    # rather than asserting which one applies.
    path = "/home/ywatanabe/proj/scitex-clew/src"
    # Act
    fix = classify_bind(path, workdir=WORKDIR, from_host=SRC).fix
    # Assert
    assert "clone it there" in fix


def test_the_agent_local_hint_names_the_host_it_exists_on() -> None:
    # Arrange: a fix without a vantage point is applied on the wrong machine.
    path = f"{WORKDIR}/runs/x"
    # Act
    fix = classify_bind(path, workdir=WORKDIR, from_host=SRC).fix
    # Assert
    assert SRC in fix


# ---------------------------------------------------------------------------
# host-infra — the nine Spartan agents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/data/gpfs/projects/punim0264/shared/corpus",
        "/mnt/c",
        "/scratch/ywatanabe/tmp",
        "/srv/models",
    ],
)
def test_a_mounted_filesystem_is_host_infrastructure(path: str) -> None:
    # Arrange
    workdir = WORKDIR
    # Act
    kind = _kind(path, workdir)
    # Assert
    assert kind == KIND_HOST_INFRA


def test_host_infrastructure_is_provisioned_rather_than_carried() -> None:
    # Arrange
    result = classify_bind("/mnt/c", workdir=WORKDIR, from_host=SRC)
    # Act
    action = result.action
    # Assert
    assert action == ACTION_PROVISION


def test_the_infra_hint_admits_the_filesystem_may_not_exist_there_at_all() -> None:
    # Arrange: shared cluster storage has no counterpart on a workstation, and
    # pretending a re-point fixes that produces a different agent, not a moved one.
    result = classify_bind("/data/gpfs/projects/x", workdir=WORKDIR, from_host=SRC)
    # Act
    fix = result.fix
    # Assert
    assert "does not exist there at all" in fix


# ---------------------------------------------------------------------------
# unclassified — the honest answer, kept honest
# ---------------------------------------------------------------------------


def test_an_unrecognisable_path_is_not_guessed_at() -> None:
    # Arrange: a confident wrong category sends the operator to provision a
    # directory that should have travelled.
    # Act
    kind = _kind("/exports/thing", workdir=WORKDIR)
    # Assert
    assert kind == KIND_UNCLASSIFIED


def test_an_unclassified_path_asks_for_a_decision_rather_than_an_action() -> None:
    # Arrange
    result = classify_bind("/exports/thing", workdir=WORKDIR, from_host=SRC)
    # Act
    action = result.action
    # Assert
    assert action == ACTION_DECIDE


def test_an_unclassified_hint_states_both_possibilities() -> None:
    # Arrange
    result = classify_bind("/exports/thing", workdir=WORKDIR, from_host=SRC)
    # Act
    fix = result.fix
    # Assert
    assert "provision it on the target" in fix and "move it with the agent" in fix


def test_a_missing_workdir_context_does_not_invent_a_category() -> None:
    # Arrange: with no workdir, a home path cannot be told from infrastructure.
    # Act
    kind = _kind("/home/ywatanabe/proj/thing", workdir="")
    # Assert
    assert kind == KIND_UNCLASSIFIED


# ---------------------------------------------------------------------------
# the component comparison, which a startswith would get wrong
# ---------------------------------------------------------------------------


def test_a_prefix_sharing_sibling_is_not_treated_as_a_child() -> None:
    # Arrange: /home/ywatanabe/proj-old starts with /home/ywatanabe/proj but is
    # a different directory. Compared component-wise for exactly this.
    # Act
    result = classify_bind(
        "/home/ywatanabe/proj-old/x", workdir="/home/ywatanabe/proj/a", from_host=SRC
    )
    # Assert
    assert result.kind == KIND_UNCLASSIFIED


def test_every_classification_carries_the_evidence_for_it() -> None:
    # Arrange: a wrong call must be arguable rather than mysterious.
    result = classify_bind("/mnt/c", workdir=WORKDIR, from_host=SRC)
    # Act
    because = result.because
    # Assert
    assert "/mnt" in because


# ---------------------------------------------------------------------------
# grouping — the order IS the value
# ---------------------------------------------------------------------------


def test_grouping_puts_provisioning_before_carrying() -> None:
    # Arrange: the operator works target-first; provisioning is usually somebody
    # standing at another machine.
    classified = classify_binds(
        (f"{WORKDIR}/runs/x", "/mnt/c"), workdir=WORKDIR, from_host=SRC
    )
    # Act
    order = [action for action, _ in group_by_action(classified)]
    # Assert
    assert order == [ACTION_PROVISION, ACTION_CARRY]


def test_grouping_omits_buckets_nothing_landed_in() -> None:
    # Arrange
    classified = classify_binds(("/mnt/c",), workdir=WORKDIR, from_host=SRC)
    # Act
    order = [action for action, _ in group_by_action(classified)]
    # Assert
    assert order == [ACTION_PROVISION]


def test_grouping_keeps_every_path() -> None:
    # Arrange: a split that drops a path is worse than no split.
    paths = (f"{WORKDIR}/runs/x", "/mnt/c", "/home/ywatanabe/.ssh", "/exports/thing")
    classified = classify_binds(paths, workdir=WORKDIR, from_host=SRC)
    # Act
    kept = {b.path for _, members in group_by_action(classified) for b in members}
    # Assert
    assert kept == set(paths)
