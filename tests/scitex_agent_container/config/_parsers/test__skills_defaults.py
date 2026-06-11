"""Tests for fleet-wide base default required-skills merge.

Pins the contract that one constant lives in
:mod:`scitex_agent_container.config._parsers._skills_defaults` and is
prepended (dedup-preserving order) to every agent's per-spec
``skills.required`` list. The operator's anti-"manage 60 packages
individually" principle (lead msg 087d779, 2026-06-11) requires a
fleet-wide skill (``scitex-todo``) to be declared ONCE and inherited
by every agent at startup; this module is that single declaration.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._skills_defaults import (
    BASE_REQUIRED_SKILLS,
    apply_base_required_skills,
)

# ---------------------------------------------------------------------------
# BASE_REQUIRED_SKILLS — pinned contents
# ---------------------------------------------------------------------------


def test_base_required_skills_is_a_tuple():
    # Arrange / Act
    value = BASE_REQUIRED_SKILLS
    # Assert
    assert isinstance(value, tuple)


def test_base_required_skills_first_entry_is_scitex_todo():
    # Arrange / Act
    first = BASE_REQUIRED_SKILLS[0]
    # Assert (operator directive: scitex-todo is fleet-wide required)
    assert first == "scitex-todo"


def test_base_required_skills_has_no_duplicates():
    # Arrange / Act
    seen = set(BASE_REQUIRED_SKILLS)
    # Assert
    assert len(seen) == len(BASE_REQUIRED_SKILLS)


# ---------------------------------------------------------------------------
# apply_base_required_skills — merge semantics
# ---------------------------------------------------------------------------


def test_apply_to_empty_spec_returns_base_defaults_in_order():
    # Arrange
    per_spec: list[str] = []
    # Act
    merged = apply_base_required_skills(per_spec)
    # Assert
    assert merged == list(BASE_REQUIRED_SKILLS)


def test_apply_prepends_base_defaults_before_per_spec_entries():
    # Arrange
    per_spec = ["alpha", "beta"]
    # Act
    merged = apply_base_required_skills(per_spec)
    # Assert
    assert merged == [*BASE_REQUIRED_SKILLS, "alpha", "beta"]


def test_apply_preserves_per_spec_relative_order():
    # Arrange
    per_spec = ["z", "a", "m"]
    # Act
    merged = apply_base_required_skills(per_spec)
    # Assert
    assert merged[len(BASE_REQUIRED_SKILLS) :] == ["z", "a", "m"]


def test_apply_dedupes_when_spec_already_lists_a_base_default():
    # Arrange — operator put scitex-todo in their per-spec list too;
    # the merged output must NOT contain a duplicate.
    per_spec = ["scitex-todo", "alpha"]
    # Act
    merged = apply_base_required_skills(per_spec)
    # Assert
    assert merged == ["scitex-todo", "alpha"]


def test_apply_dedupe_keeps_base_default_position_first():
    # Arrange
    per_spec = ["alpha", "scitex-todo"]
    # Act
    merged = apply_base_required_skills(per_spec)
    # Assert (base position wins; per-spec duplicate dropped)
    assert merged == ["scitex-todo", "alpha"]


def test_apply_is_idempotent():
    # Arrange
    per_spec = ["alpha"]
    # Act
    once = apply_base_required_skills(per_spec)
    twice = apply_base_required_skills(once)
    # Assert
    assert once == twice


def test_apply_returns_a_new_list_does_not_mutate_input():
    # Arrange
    per_spec = ["alpha", "beta"]
    snapshot = list(per_spec)
    # Act
    _ = apply_base_required_skills(per_spec)
    # Assert
    assert per_spec == snapshot


def test_apply_dedupes_duplicates_within_per_spec_list():
    # Arrange — defensive: a per-spec list with its own duplicates
    # should collapse, not propagate.
    per_spec = ["alpha", "alpha", "beta"]
    # Act
    merged = apply_base_required_skills(per_spec)
    # Assert
    assert merged == [*BASE_REQUIRED_SKILLS, "alpha", "beta"]


# ---------------------------------------------------------------------------
# Type contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "per_spec",
    [
        [],
        ["alpha"],
        ["alpha", "beta", "gamma"],
        ["scitex-todo"],
    ],
)
def test_apply_returns_list_of_strings(per_spec):
    # Arrange / Act
    merged = apply_base_required_skills(per_spec)
    # Assert
    assert all(isinstance(s, str) for s in merged)
