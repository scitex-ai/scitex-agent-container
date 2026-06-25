"""Named-group resolver (group-based a2a ACL, operator 2026-06-25).

Pure string-in / string-out resolution — no DB, no fixtures. Covers:

* An explicit ``metadata.labels.group`` wins verbatim.
* An absent group label derives the group from the role: the
  developer-ish roles (project-maintainer / maintainer / dev-agent /
  contributor, and project-suffixed forms) default to ``developer``.
* Anything else → ``""`` (ungrouped).
* :func:`group_from_labels` reads the ``group`` / ``role`` keys.
* :func:`is_developer_group` recognises the privileged group.

AAA, one assertion per test, no mocks.
"""

from __future__ import annotations

from scitex_agent_container.config._group_resolver import (
    DEVELOPER_GROUP,
    group_from_labels,
    groups_peered,
    is_developer_group,
    resolve_group,
)


def test_explicit_group_label_wins_over_role() -> None:
    # Arrange
    label = "analysts"
    # Act
    group = resolve_group(group_label=label, role="contributor")
    # Assert
    assert group == "analysts"


def test_explicit_group_label_is_whitespace_trimmed() -> None:
    # Arrange
    label = "  analysts  "
    # Act
    group = resolve_group(group_label=label, role=None)
    # Assert
    assert group == "analysts"


def test_role_project_maintainer_derives_developer_group() -> None:
    # Arrange
    role = "project-maintainer"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == DEVELOPER_GROUP


def test_role_maintainer_derives_developer_group() -> None:
    # Arrange
    role = "maintainer"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == DEVELOPER_GROUP


def test_role_dev_agent_derives_developer_group() -> None:
    # Arrange
    role = "dev-agent"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == DEVELOPER_GROUP


def test_role_contributor_derives_developer_group() -> None:
    # Arrange
    role = "contributor"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == DEVELOPER_GROUP


def test_project_suffixed_contributor_role_derives_developer_group() -> None:
    # Arrange
    role = "contributor-figrecipe"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == DEVELOPER_GROUP


def test_role_match_is_case_insensitive() -> None:
    # Arrange
    role = "Project-Maintainer"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == DEVELOPER_GROUP


def test_non_developer_role_is_ungrouped() -> None:
    # Arrange
    role = "experiment-capsule"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == ""


def test_empty_group_label_falls_through_to_role() -> None:
    # Arrange
    label = "   "
    # Act
    group = resolve_group(group_label=label, role="contributor")
    # Assert
    assert group == DEVELOPER_GROUP


def test_no_group_and_no_role_is_ungrouped() -> None:
    # Arrange
    # Act
    group = resolve_group(group_label=None, role=None)
    # Assert
    assert group == ""


def test_group_from_labels_reads_explicit_group_key() -> None:
    # Arrange
    labels = {"group": "analysts", "role": "contributor"}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == "analysts"


def test_group_from_labels_derives_from_role_key() -> None:
    # Arrange
    labels = {"role": "dev-agent"}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == DEVELOPER_GROUP


def test_group_from_labels_none_is_ungrouped() -> None:
    # Arrange
    labels = None
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == ""


def test_is_developer_group_true_for_developer() -> None:
    # Arrange
    group = "developer"
    # Act
    result = is_developer_group(group)
    # Assert
    assert result is True


def test_is_developer_group_false_for_other_group() -> None:
    # Arrange
    group = "analysts"
    # Act
    result = is_developer_group(group)
    # Assert
    assert result is False


def test_is_developer_group_false_for_empty() -> None:
    # Arrange
    group = ""
    # Act
    result = is_developer_group(group)
    # Assert
    assert result is False


def test_scientist_and_developer_are_peered() -> None:
    # Arrange
    a, b = "scientist", "developer"
    # Act
    result = groups_peered(a, b)
    # Assert
    assert result is True


def test_peering_is_symmetric() -> None:
    # Arrange
    a, b = "developer", "scientist"
    # Act
    result = groups_peered(a, b)
    # Assert
    assert result is True


def test_unrelated_group_not_peered_with_developer() -> None:
    # Arrange
    a, b = "analysts", "developer"
    # Act
    result = groups_peered(a, b)
    # Assert
    assert result is False


def test_group_not_peered_with_itself() -> None:
    # Arrange
    a, b = "scientist", "scientist"
    # Act
    result = groups_peered(a, b)
    # Assert
    assert result is False
