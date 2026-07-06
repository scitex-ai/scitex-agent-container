"""Unit tests for ``a2a/_card_identity.py`` — the spec → identity projection.

``spec_identity`` is the single source of "who is this agent" shared by
both a2a discovery surfaces (the AgentCard and the ``a2a peers`` rows), so
its contract is pinned here with pure inputs — no mocks, no I/O.

STX-TQ002 AAA markers + STX-TQ007 one-assert-per-test. Test names spell
out the behaviour being verified (TQ003).
"""

from __future__ import annotations

from scitex_agent_container.a2a._card_identity import as_str_list, spec_identity

# ---------------------------------------------------------------------------
# as_str_list — CSV / list / junk coercion
# ---------------------------------------------------------------------------


def test_as_str_list_splits_and_strips_csv_string() -> None:
    # Arrange
    value = "triage CI,  review PRs , "
    # Act
    result = as_str_list(value)
    # Assert
    assert result == ["triage CI", "review PRs"]


def test_as_str_list_filters_blanks_from_list() -> None:
    # Arrange
    value = ["a", "  ", "b", None]
    # Act
    result = as_str_list(value)
    # Assert
    assert result == ["a", "b"]


def test_as_str_list_returns_empty_for_none() -> None:
    # Arrange
    value = None
    # Act
    result = as_str_list(value)
    # Assert
    assert result == []


def test_as_str_list_returns_empty_for_dict() -> None:
    # Arrange
    value = {"x": 1}
    # Act
    result = as_str_list(value)
    # Assert
    assert result == []


# ---------------------------------------------------------------------------
# spec_identity — role headline
# ---------------------------------------------------------------------------


def test_spec_identity_reads_role_string_from_labels() -> None:
    # Arrange
    v3 = {"metadata": {"labels": {"role": "project-maintainer"}}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["role"] == "project-maintainer"


def test_spec_identity_multi_role_list_stays_a_list() -> None:
    # Arrange — a (future) multi-role spec must not crash and keeps the list.
    v3 = {"metadata": {"labels": {"role": ["maintainer", "reviewer"]}}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["role"] == ["maintainer", "reviewer"]


def test_spec_identity_single_element_role_list_collapses_to_string() -> None:
    # Arrange
    v3 = {"metadata": {"labels": {"role": ["solo"]}}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["role"] == "solo"


def test_spec_identity_omits_role_when_absent() -> None:
    # Arrange
    v3 = {"metadata": {"labels": {}}, "spec": {}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert "role" not in identity


# ---------------------------------------------------------------------------
# spec_identity — responsibilities bullets
# ---------------------------------------------------------------------------


def test_spec_identity_reads_responsibilities_from_spec_list() -> None:
    # Arrange
    v3 = {"spec": {"responsibilities": ["triage CI", "review PRs"]}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["responsibilities"] == ["triage CI", "review PRs"]


def test_spec_identity_prefers_responsibilities_from_extensions() -> None:
    # Arrange — spec.extensions.responsibilities is the schema-valid slot.
    v3 = {"spec": {"extensions": {"responsibilities": ["own X", "own Y"]}}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["responsibilities"] == ["own X", "own Y"]


def test_spec_identity_responsibilities_fall_back_to_labels_csv() -> None:
    # Arrange — no spec.responsibilities; labels carries a CSV instead.
    v3 = {"metadata": {"labels": {"responsibilities": "a, b"}}, "spec": {}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["responsibilities"] == ["a", "b"]


def test_spec_identity_omits_responsibilities_when_absent() -> None:
    # Arrange
    v3 = {"metadata": {"labels": {"role": "worker"}}, "spec": {}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert "responsibilities" not in identity


# ---------------------------------------------------------------------------
# spec_identity — groups / purpose / project
# ---------------------------------------------------------------------------


def test_spec_identity_reads_groups_plural_list() -> None:
    # Arrange
    v3 = {"metadata": {"labels": {"groups": ["sac", "fleet"]}}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["groups"] == ["sac", "fleet"]


def test_spec_identity_groups_fall_back_to_singular_group() -> None:
    # Arrange — no plural ``groups``; a singular ``group`` string is used.
    v3 = {"metadata": {"labels": {"group": "developers"}}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["groups"] == ["developers"]


def test_spec_identity_reads_purpose_string() -> None:
    # Arrange
    v3 = {"metadata": {"labels": {"purpose": "keep sac green"}}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["purpose"] == "keep sac green"


def test_spec_identity_reads_project_from_workdir_basename() -> None:
    # Arrange
    v3 = {"spec": {"workdir": "/home/u/proj/scitex-dev/"}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity["project"] == "scitex-dev"


def test_spec_identity_omits_project_when_no_workdir() -> None:
    # Arrange
    v3 = {"spec": {"runtime": "apptainer"}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert "project" not in identity


# ---------------------------------------------------------------------------
# spec_identity — defensive input handling (best-effort, never crashes)
# ---------------------------------------------------------------------------


def test_spec_identity_returns_empty_for_non_dict_input() -> None:
    # Arrange
    value = None
    # Act
    identity = spec_identity(value)  # type: ignore[arg-type]
    # Assert
    assert identity == {}


def test_spec_identity_tolerates_non_dict_labels() -> None:
    # Arrange — a malformed spec whose labels is a list, not a mapping.
    v3 = {"metadata": {"labels": ["oops"]}, "spec": {"workdir": "/x/repo"}}
    # Act
    identity = spec_identity(v3)
    # Assert
    assert identity == {"project": "repo"}
