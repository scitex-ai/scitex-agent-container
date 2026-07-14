"""Named-group resolver (group-based a2a ACL, operator 2026-06-25).

Pure string-in / string-out resolution — no DB, no fixtures. Covers:

* An explicit ``metadata.labels.group`` wins verbatim.
* An absent group label derives the group from the role: the
  developer-ish roles (project-maintainer / maintainer / dev-agent /
  contributor, and project-suffixed forms) default to ``developer``.
* Anything else → ``""`` (ungrouped).
* :func:`group_from_labels` reads the PLURAL ``groups`` list (spec
  convention, first non-empty element wins), the SINGULAR ``group``
  string (wins over the list), or falls back to the ``role`` key.
* :func:`is_developer_group` recognises the privileged group.

AAA, one assertion per test, no mocks.
"""

from __future__ import annotations

from scitex_agent_container.config._group_resolver import (
    DEVELOPER_GROUP,
    GENERALIST_GROUP,
    PRIVILEGED_GROUP,
    RESEARCHER_GROUP,
    all_named_groups,
    group_from_labels,
    groups_mesh,
    is_developer_group,
    is_mesh_group,
    is_research_group,
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


# ---------------------------------------------------------------------------
# Researcher role-derivation (operator ruling, nv-spawn-acl-incident:
# developer AND researcher roles must BOTH be able to spawn / restart peers).
#
# The developer group has always had a role-derivation path, so a dev agent
# joins ``developer`` with no spec edit. The researcher group had NONE: a spec
# carrying ``role: researcher`` but no explicit ``groups:`` label resolved to
# ``""`` (ungrouped) — and an ungrouped caller is denied BOTH spawn and
# cross-group manage. These pin the symmetric derivation.
# ---------------------------------------------------------------------------


def test_role_researcher_derives_researcher_group() -> None:
    # Arrange
    role = "researcher"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == RESEARCHER_GROUP


def test_role_research_agent_derives_researcher_group() -> None:
    # Arrange
    role = "research-agent"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == RESEARCHER_GROUP


def test_role_scientist_derives_researcher_group() -> None:
    # Arrange
    role = "scientist"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == RESEARCHER_GROUP


def test_project_suffixed_research_agent_role_derives_researcher_group() -> None:
    # Arrange
    role = "research-agent-neurovista"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == RESEARCHER_GROUP


def test_researcher_role_match_is_case_insensitive() -> None:
    # Arrange
    role = "Research-Agent"
    # Act
    group = resolve_group(group_label=None, role=role)
    # Assert
    assert group == RESEARCHER_GROUP


def test_explicit_group_label_wins_over_researcher_role() -> None:
    """Isolation guard: an explicit label still beats the new role default.

    A deliberately-isolated solver authored as ``role: researcher`` +
    ``groups: [solver]`` must STAY in ``solver`` (a non-mesh group), so
    role-derivation can never silently promote it into the fleet mesh.
    """
    # Arrange
    label = "solver"
    # Act
    group = resolve_group(group_label=label, role="researcher")
    # Assert
    assert group == label


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


def test_group_from_labels_reads_plural_groups_researcher() -> None:
    # Arrange
    labels = {"groups": ["researcher"]}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == "researcher"


def test_group_from_labels_reads_plural_groups_generalist() -> None:
    # Arrange
    labels = {"groups": ["generalist"]}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == "generalist"


def test_group_from_labels_singular_group_still_works() -> None:
    # Arrange
    labels = {"group": "developer"}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == "developer"


def test_group_from_labels_singular_wins_over_plural() -> None:
    # Arrange
    labels = {"group": "developer", "groups": ["researcher"]}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == "developer"


def test_group_from_labels_empty_plural_falls_back_to_role() -> None:
    # Arrange
    labels = {"groups": [], "role": "dev-agent"}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == DEVELOPER_GROUP


def test_group_from_labels_empty_plural_no_role_is_ungrouped() -> None:
    # Arrange
    labels = {"groups": []}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == ""


def test_group_from_labels_plural_beats_role() -> None:
    # Arrange
    labels = {"groups": ["scientist"], "role": "project-maintainer"}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == "scientist"


def test_group_from_labels_first_nonempty_plural_element_wins() -> None:
    # Arrange
    labels = {"groups": ["  ", "researcher", "generalist"]}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == "researcher"


def test_group_from_labels_non_list_plural_falls_back_to_role() -> None:
    # Arrange
    labels = {"groups": "researcher", "role": "dev-agent"}
    # Act
    group = group_from_labels(labels)
    # Assert
    assert group == DEVELOPER_GROUP


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


def test_is_research_group_true_for_researcher() -> None:
    # Arrange
    group = "researcher"
    # Act
    result = is_research_group(group)
    # Assert
    assert result is True


def test_is_research_group_false_for_other_group() -> None:
    # Arrange
    group = "analysts"
    # Act
    result = is_research_group(group)
    # Assert
    assert result is False


def test_is_research_group_false_for_empty() -> None:
    # Arrange
    group = ""
    # Act
    result = is_research_group(group)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# cross-group mesh predicates (operator 2026-06-27)
# ---------------------------------------------------------------------------


def test_is_mesh_group_true_for_researcher() -> None:
    # Arrange
    group = RESEARCHER_GROUP
    # Act
    result = is_mesh_group(group)
    # Assert
    assert result is True


def test_is_mesh_group_true_for_generalist() -> None:
    # Arrange
    group = GENERALIST_GROUP
    # Act
    result = is_mesh_group(group)
    # Assert
    assert result is True


def test_is_mesh_group_false_for_solver() -> None:
    # Arrange
    group = "solver"
    # Act
    result = is_mesh_group(group)
    # Assert
    assert result is False


def test_groups_mesh_true_across_standard_groups() -> None:
    # Arrange
    # Act
    result = groups_mesh(DEVELOPER_GROUP, RESEARCHER_GROUP)
    # Assert
    assert result is True


def test_groups_mesh_false_when_one_side_is_non_mesh() -> None:
    # Arrange
    # Act
    result = groups_mesh(DEVELOPER_GROUP, "solver")
    # Assert
    assert result is False


def test_groups_mesh_false_when_one_side_is_ungrouped() -> None:
    # Arrange
    # Act
    result = groups_mesh(RESEARCHER_GROUP, "")
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# all_named_groups (bulk lifecycle selection, e.g. ``start --group``) — the
# MULTI-value cut deliberately distinct from group_from_labels' ACL-effective
# singular pick.
# ---------------------------------------------------------------------------


def test_all_named_groups_none_labels_is_empty() -> None:
    # Arrange
    labels = None
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset()


def test_all_named_groups_empty_labels_is_empty() -> None:
    # Arrange
    labels = {}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset()


def test_all_named_groups_returns_every_plural_element() -> None:
    # Arrange — grant-style spec: belongs to all four at once.
    labels = {"groups": ["generalist", "privileged", "developer", "researcher"]}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset({"generalist", "privileged", "developer", "researcher"})


def test_all_named_groups_includes_developer_even_when_not_first() -> None:
    # Arrange — the ACL-effective pick (group_from_labels) would only see
    # "generalist"; the multi-value reader must still surface "developer".
    labels = {"groups": ["generalist", "privileged", "developer", "researcher"]}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert "developer" in groups


def test_all_named_groups_includes_singular_group_key() -> None:
    # Arrange
    labels = {"group": "analysts"}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert "analysts" in groups


def test_all_named_groups_unions_singular_and_plural() -> None:
    # Arrange
    labels = {"group": "analysts", "groups": ["developer"]}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset({"analysts", "developer"})


def test_all_named_groups_strips_whitespace() -> None:
    # Arrange
    labels = {"groups": ["  active  "]}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset({"active"})


def test_all_named_groups_skips_blank_elements() -> None:
    # Arrange
    labels = {"groups": ["developer", "   ", ""]}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset({"developer"})


def test_all_named_groups_non_list_plural_string_is_honoured() -> None:
    # Arrange — defensive: a bare string (not the authored list form) is
    # still treated as one group name rather than silently dropped.
    labels = {"groups": "researcher"}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset({"researcher"})


def test_all_named_groups_malformed_plural_int_is_ignored() -> None:
    # Arrange — defensive: an unexpected scalar type must not raise.
    labels = {"groups": 42}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset()


def test_all_named_groups_does_not_derive_from_role() -> None:
    # Arrange — unlike group_from_labels, no role-derivation fallback:
    # this reader answers "what does the spec explicitly list", period.
    labels = {"role": "dev-agent"}
    # Act
    groups = all_named_groups(labels)
    # Assert
    assert groups == frozenset()


# ---------------------------------------------------------------------------
# privileged group (operator 2026-07-02): host-exec eligible + mesh member so
# it can manage agents across groups
# ---------------------------------------------------------------------------


def test_is_mesh_group_true_for_privileged() -> None:
    # Arrange
    group = PRIVILEGED_GROUP
    # Act
    result = is_mesh_group(group)
    # Assert
    assert result is True


def test_groups_mesh_true_privileged_manages_developer() -> None:
    # Arrange — privileged meshes with the standard fleet, so a privileged
    # caller may manage (start / restart / …) a developer target cross-group.
    # Act
    result = groups_mesh(PRIVILEGED_GROUP, DEVELOPER_GROUP)
    # Assert
    assert result is True


def test_groups_mesh_true_privileged_manages_researcher() -> None:
    # Arrange
    # Act
    result = groups_mesh(PRIVILEGED_GROUP, RESEARCHER_GROUP)
    # Assert
    assert result is True


def test_groups_mesh_false_privileged_against_non_mesh_group() -> None:
    # Arrange — an isolated solver stays unmanageable even by privileged.
    # Act
    result = groups_mesh(PRIVILEGED_GROUP, "solver")
    # Assert
    assert result is False
