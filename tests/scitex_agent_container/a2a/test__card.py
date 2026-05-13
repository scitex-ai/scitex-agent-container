"""Regression tests for ``a2a/_card.py::project_card``.

The card was reading two stale spec paths that v3 had moved:

* ``spec.skills.required`` — v3 rejects ``spec.skills`` outright (skills
  live in ``dot_claude/skills/``). The card surface lost the required-
  skills list silently for every v3 agent.
* ``spec.model`` — v3 moved the model under ``spec.claude.model``. The
  card's ``x-scitex-agent-container.model`` field was always ``None``.

These tests pin the corrected behavior + the back-compat fallbacks so
the bugs cannot regress.
"""

from __future__ import annotations

from scitex_agent_container.a2a._card import project_card

# ---------------------------------------------------------------------------
# model field — v3 location wins over legacy v2
# ---------------------------------------------------------------------------


def test_model_read_from_spec_claude_v3() -> None:
    v3 = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {"labels": {"role": "worker"}},
        "spec": {"runtime": "apptainer", "claude": {"model": "sonnet"}},
    }
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    assert card["x-scitex-agent-container"]["model"] == "sonnet"


def test_model_legacy_spec_model_back_compat() -> None:
    """v2 YAMLs with top-level spec.model still surface in the card."""
    v3 = {
        "apiVersion": "scitex-agent-container/v2",
        "metadata": {"labels": {"role": "worker"}},
        "spec": {"runtime": "apptainer", "model": "haiku"},
    }
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    assert card["x-scitex-agent-container"]["model"] == "haiku"


def test_model_v3_takes_precedence_when_both_present() -> None:
    v3 = {
        "spec": {"model": "haiku", "claude": {"model": "sonnet"}},
    }
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    assert card["x-scitex-agent-container"]["model"] == "sonnet"


def test_model_missing_is_none() -> None:
    v3 = {"spec": {"runtime": "apptainer"}}
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    assert card["x-scitex-agent-container"]["model"] is None


# ---------------------------------------------------------------------------
# required_skills — labels.skills CSV is the new home; legacy still accepted
# ---------------------------------------------------------------------------


def test_required_skills_from_labels_csv() -> None:
    """v3-native path: declare skills via metadata.labels.skills (CSV)."""
    v3 = {
        "metadata": {
            "labels": {
                "role": "researcher",
                "skills": "scitex-dev, gh-cli,  git",
            }
        },
        "spec": {"runtime": "apptainer"},
    }
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    ext_skills = card["x-scitex-agent-container"]["required_skills"]
    assert ext_skills == ["scitex-dev", "gh-cli", "git"]
    # Also appears in skills[0].tags (union with capabilities)
    tags = card["skills"][0]["tags"]
    for s in ("scitex-dev", "gh-cli", "git"):
        assert s in tags


def test_required_skills_legacy_spec_skills_back_compat() -> None:
    """Pre-validation legacy spec.skills.required still flows through."""
    v3 = {
        "metadata": {"labels": {"role": "worker"}},
        "spec": {"skills": {"required": ["foo", "bar"]}},
    }
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    assert card["x-scitex-agent-container"]["required_skills"] == ["foo", "bar"]


def test_required_skills_labels_and_legacy_merge() -> None:
    """Operator using BOTH (mid-migration) gets the union in the card."""
    v3 = {
        "metadata": {"labels": {"skills": "new1,new2"}},
        "spec": {"skills": {"required": ["old1"]}},
    }
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    skills = card["x-scitex-agent-container"]["required_skills"]
    assert sorted(skills) == ["new1", "new2", "old1"]


def test_required_skills_empty_when_neither_set() -> None:
    v3 = {"metadata": {"labels": {"role": "x"}}, "spec": {}}
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    assert card["x-scitex-agent-container"]["required_skills"] == []


def test_capabilities_and_skills_unioned_in_tags() -> None:
    """skills[0].tags must include BOTH labels.capabilities AND skills,
    deduplicated and sorted."""
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
    card = project_card("alpha", v3, "http://127.0.0.1:7901")
    tags = card["skills"][0]["tags"]
    assert tags == sorted(set(["audit", "git", "scitex-dev"]))
