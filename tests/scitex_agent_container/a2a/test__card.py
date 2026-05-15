"""Regression tests for ``a2a/_card.py::project_card``.

The card was reading two stale spec paths that v3 had moved:

* ``spec.skills.required`` — v3 rejects ``spec.skills`` outright (skills
  live in ``dot_claude/skills/``). The card surface lost the required-
  skills list silently for every v3 agent.
* ``spec.model`` — v3 moved the model under ``spec.claude.model``. The
  card's ``x-scitex-agent-container.model`` field was always ``None``.

These tests pin the corrected behavior + the back-compat fallbacks so
the bugs cannot regress.

TQ cleanup: every test carries AAA markers (TQ002) and asserts exactly
one fact (TQ007). Same-shape invariants over a small input matrix
collapse into ``pytest.parametrize``. Test names spell out the
behaviour being verified (TQ003-compatible).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.a2a._card import project_card

# ---------------------------------------------------------------------------
# model field — v3 location wins over legacy v2
# ---------------------------------------------------------------------------


def test_model_read_from_spec_claude_v3() -> None:
    # Arrange
    v3 = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {"labels": {"role": "worker"}},
        "spec": {"runtime": "apptainer", "claude": {"model": "sonnet"}},
    }
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert card["x-scitex-agent-container"]["model"] == "sonnet"


def test_model_legacy_spec_model_back_compat() -> None:
    """v2 YAMLs with top-level spec.model still surface in the card."""
    # Arrange
    v3 = {
        "apiVersion": "scitex-agent-container/v2",
        "metadata": {"labels": {"role": "worker"}},
        "spec": {"runtime": "apptainer", "model": "haiku"},
    }
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert card["x-scitex-agent-container"]["model"] == "haiku"


def test_model_v3_takes_precedence_when_both_present() -> None:
    # Arrange
    v3 = {
        "spec": {"model": "haiku", "claude": {"model": "sonnet"}},
    }
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert card["x-scitex-agent-container"]["model"] == "sonnet"


def test_model_missing_is_none() -> None:
    # Arrange
    v3 = {"spec": {"runtime": "apptainer"}}
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert card["x-scitex-agent-container"]["model"] is None


# ---------------------------------------------------------------------------
# required_skills — labels.skills CSV is the new home; legacy still accepted
# ---------------------------------------------------------------------------


@pytest.fixture
def card_from_labels_csv() -> dict:
    """Card built from a v3 spec that declares skills via labels.skills CSV."""
    v3 = {
        "metadata": {
            "labels": {
                "role": "researcher",
                "skills": "scitex-dev, gh-cli,  git",
            }
        },
        "spec": {"runtime": "apptainer"},
    }
    return project_card("alpha", v3, "http://127.0.0.1:7901")


def test_required_skills_from_labels_csv_parses_and_strips(
    card_from_labels_csv: dict,
) -> None:
    """v3-native path: labels.skills CSV becomes the required_skills list."""
    # Arrange
    card = card_from_labels_csv
    # Act
    ext_skills = card["x-scitex-agent-container"]["required_skills"]
    # Assert
    assert ext_skills == ["scitex-dev", "gh-cli", "git"]


@pytest.mark.parametrize("skill", ["scitex-dev", "gh-cli", "git"])
def test_required_skills_from_labels_csv_appear_in_skills_tags(
    card_from_labels_csv: dict, skill: str
) -> None:
    """Each declared skill is unioned into ``skills[0].tags``."""
    # Arrange
    card = card_from_labels_csv
    # Act
    tags = card["skills"][0]["tags"]
    # Assert
    assert skill in tags


def test_required_skills_legacy_spec_skills_back_compat() -> None:
    """Pre-validation legacy spec.skills.required still flows through."""
    # Arrange
    v3 = {
        "metadata": {"labels": {"role": "worker"}},
        "spec": {"skills": {"required": ["foo", "bar"]}},
    }
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert card["x-scitex-agent-container"]["required_skills"] == ["foo", "bar"]


def test_required_skills_labels_and_legacy_merge() -> None:
    """Operator using BOTH (mid-migration) gets the union in the card."""
    # Arrange
    v3 = {
        "metadata": {"labels": {"skills": "new1,new2"}},
        "spec": {"skills": {"required": ["old1"]}},
    }
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert sorted(card["x-scitex-agent-container"]["required_skills"]) == [
        "new1",
        "new2",
        "old1",
    ]


def test_required_skills_empty_when_neither_set() -> None:
    # Arrange
    v3 = {"metadata": {"labels": {"role": "x"}}, "spec": {}}
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert card["x-scitex-agent-container"]["required_skills"] == []


def test_capabilities_and_skills_unioned_in_tags() -> None:
    """skills[0].tags must include BOTH labels.capabilities AND skills,
    deduplicated and sorted."""
    # Arrange
    v3 = {
        "metadata": {
            "labels": {
                "role": "worker",
                "capabilities": "audit,git",
                "skills": "git,scitex-dev",  # 'git' duplicates capabilities
            }
        },
        "spec": {},
    }
    # Act
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    # Assert
    assert card["skills"][0]["tags"] == sorted({"audit", "git", "scitex-dev"})


# ---------------------------------------------------------------------------
# D3 — structured isolation block
# (docs/adr/0001-isolation-hardening.md)
# ---------------------------------------------------------------------------


def _iso(v3: dict) -> dict:
    return project_card("alpha", v3, "http://127.0.0.1:7901")[
        "x-scitex-agent-container"
    ]["isolation"]


@pytest.fixture
def default_isolation() -> dict:
    """Isolation block produced from an empty spec (the hardened default)."""
    return _iso({"spec": {}})


@pytest.mark.parametrize(
    "field,expected",
    [
        ("level", "hardened"),
        ("containall", True),
        ("cleanenv", True),
        ("writable_tmpfs", True),
        ("preflight_passed", ["uid-nonzero", "no-host-home"]),
        ("preflight_allowed", []),
        ("binds_count", 0),
        ("binds_writable_count", 0),
    ],
)
def test_isolation_default_yaml_is_hardened(
    default_isolation: dict, field: str, expected
) -> None:
    """Empty spec → level=hardened, all defensive booleans true."""
    # Arrange
    iso = default_isolation
    # Act
    value = iso[field]
    # Assert
    assert value == expected


@pytest.fixture
def relaxed_isolation() -> dict:
    """Isolation block produced when ``apptainer.relaxed`` is true."""
    return _iso({"spec": {"apptainer": {"relaxed": True}}})


@pytest.mark.parametrize(
    "field,expected",
    [
        ("level", "relaxed"),
        ("containall", False),
        ("cleanenv", False),
        ("writable_tmpfs", False),
        ("preflight_passed", []),
    ],
)
def test_isolation_relaxed_true_flips_all_booleans(
    relaxed_isolation: dict, field: str, expected
) -> None:
    # Arrange
    iso = relaxed_isolation
    # Act
    value = iso[field]
    # Assert
    assert value == expected


def test_isolation_operator_declared_cleanenv_keeps_hardened_level() -> None:
    """Operator put --cleanenv in raw_args — level stays hardened."""
    # Arrange
    spec = {"spec": {"apptainer": {"raw_args": ["--cleanenv"]}}}
    # Act
    iso = _iso(spec)
    # Assert
    assert iso["level"] == "hardened"


def test_isolation_operator_declared_cleanenv_sets_cleanenv_true() -> None:
    """Operator put --cleanenv in raw_args — cleanenv flag reflects it."""
    # Arrange
    spec = {"spec": {"apptainer": {"raw_args": ["--cleanenv"]}}}
    # Act
    iso = _iso(spec)
    # Assert
    assert iso["cleanenv"] is True


@pytest.fixture
def overlay_isolation() -> dict:
    """Isolation block produced when an overlay image is configured."""
    return _iso({"spec": {"apptainer": {"overlay": "/tmp/ov.img"}}})


@pytest.mark.parametrize(
    "field,expected",
    [
        ("level", "hardened"),
        ("writable_tmpfs", False),
        # containall + cleanenv unaffected by overlay
        ("containall", True),
        ("cleanenv", True),
    ],
)
def test_isolation_overlay_disables_writable_tmpfs_but_stays_hardened(
    overlay_isolation: dict, field: str, expected
) -> None:
    # Arrange
    iso = overlay_isolation
    # Act
    value = iso[field]
    # Assert
    assert value == expected


@pytest.fixture
def binds_isolation() -> dict:
    """Isolation block produced with a mixed ro/rw binds list."""
    return _iso(
        {
            "spec": {
                "apptainer": {
                    "binds": [
                        "/srv/a:/srv/a:ro",
                        "/srv/b:/srv/b:ro",
                        "/srv/c:/srv/c",  # rw (no :ro)
                    ]
                }
            }
        }
    )


def test_isolation_binds_count_populates_from_apptainer_binds(
    binds_isolation: dict,
) -> None:
    # Arrange
    iso = binds_isolation
    # Act
    count = iso["binds_count"]
    # Assert
    assert count == 3


def test_isolation_binds_writable_count_excludes_ro_entries(
    binds_isolation: dict,
) -> None:
    # Arrange
    iso = binds_isolation
    # Act
    writable = iso["binds_writable_count"]
    # Assert
    assert writable == 1


@pytest.fixture
def preflight_allow_isolation() -> dict:
    """Isolation block produced when ``preflight_allow`` escape hatch is set."""
    return _iso(
        {
            "spec": {
                "apptainer": {
                    "preflight_allow": ["$HOME/.gitconfig"],
                }
            }
        }
    )


def test_isolation_preflight_allow_downgrades_level_to_custom(
    preflight_allow_isolation: dict,
) -> None:
    """``preflight_allow: [...]`` downgrades level→custom."""
    # Arrange
    iso = preflight_allow_isolation
    # Act
    level = iso["level"]
    # Assert
    assert level == "custom"


def test_isolation_preflight_allow_surfaces_in_preflight_allowed(
    preflight_allow_isolation: dict,
) -> None:
    """``preflight_allow: [...]`` entries appear verbatim in preflight_allowed."""
    # Arrange
    iso = preflight_allow_isolation
    # Act
    allowed = iso["preflight_allowed"]
    # Assert
    assert allowed == ["$HOME/.gitconfig"]
