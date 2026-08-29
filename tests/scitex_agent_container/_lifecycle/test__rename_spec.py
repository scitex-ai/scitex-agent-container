"""The spec rewriter must find every self-reference and corrupt nothing.

Pure functions over YAML text — no filesystem, no fleet. The fixture spec
is the live project-maintainer shape (relaxed apptainer + directory
overlay + raw_args env block), comments and all, so "did the rename find
the overlay path buried in a flat raw_args list?" is a real question here
and not a toy one.
"""

from __future__ import annotations

import pytest
import yaml

from scitex_agent_container._lifecycle._rename_spec import (
    SpecRewriteError,
    rewrite_spec,
    sub_bind,
    sub_env_value,
    sub_path,
    sub_token,
)

from .._helpers.fleet_root import make_spec

OLD = "scitex-todo"
NEW = "scitex-cards"


@pytest.fixture
def rewritten() -> str:
    text, _changes = rewrite_spec(make_spec(OLD), OLD, NEW)
    return text


@pytest.fixture
def doc(rewritten: str) -> dict:
    return yaml.safe_load(rewritten)


@pytest.fixture
def raw_args(doc: dict) -> list[str]:
    return doc["spec"]["apptainer"]["raw_args"]


# ---------------------------------------------------------------------------
# Value-level rules
# ---------------------------------------------------------------------------


def test_sub_token_rewrites_a_prefix_before_a_hyphen():
    """``<old>-maintainer`` is the fleet's purpose-label convention."""
    # Arrange
    value = "scitex-todo-maintainer"
    # Act
    result = sub_token(value, OLD, NEW)
    # Assert
    assert result == "scitex-cards-maintainer"


def test_sub_token_leaves_a_name_embedded_in_a_longer_word_alone():
    """A boundary rule, not a substring sed — this is the anti-corruption bit."""
    # Arrange
    value = "xscitex-todoy"
    # Act
    result = sub_token(value, OLD, NEW)
    # Assert
    assert result == "xscitex-todoy"


def test_sub_path_rewrites_a_whole_path_component():
    # Arrange
    value = "/home/u/proj/scitex-todo"
    # Act
    result = sub_path(value, OLD, NEW)
    # Assert
    assert result == "/home/u/proj/scitex-cards"


def test_sub_path_leaves_a_component_that_merely_contains_the_name():
    """``scitex-todo-archive`` is a DIFFERENT directory. Do not touch it."""
    # Arrange
    value = "/home/u/proj/scitex-todo-archive/x"
    # Act
    result = sub_path(value, OLD, NEW)
    # Assert
    assert result == "/home/u/proj/scitex-todo-archive/x"


def test_sub_bind_preserves_the_mount_option():
    # Arrange
    value = "/home/u/proj/scitex-todo:/home/u/proj/scitex-todo:ro"
    # Act
    result = sub_bind(value, OLD, NEW)
    # Assert
    assert result == "/home/u/proj/scitex-cards:/home/u/proj/scitex-cards:ro"


def test_sub_env_value_rewrites_the_board_identity():
    # Arrange
    key = "SCITEX_TODO_AGENT_ID"
    # Act
    result = sub_env_value(key, OLD, OLD, NEW)
    # Assert
    assert result == NEW


def test_sub_env_value_rewrites_the_CURRENT_board_identity():
    """The spelling 108 specs already use, and it was not in ENV_RULES.

    Measured 2026-08-19 on compute-04: 108 specs declare
    ``SCITEX_CARDS_AGENT_ID`` and 193 still declare the old
    ``SCITEX_TODO_AGENT_ID``. Only the old key was listed, so renaming any of
    those 108 agents left the board identity pointing at the agent's FORMER
    name, and every card it then wrote was attributed to an agent that no
    longer exists -- the damage this module's own docstring warns about.
    """
    # Arrange
    key = "SCITEX_CARDS_AGENT_ID"
    # Act
    result = sub_env_value(key, OLD, OLD, NEW)
    # Assert
    assert result == NEW


def test_sub_env_value_still_rewrites_the_legacy_board_identity():
    """Both spellings, on purpose, while both populations exist.

    Not a compatibility fallback to be tidied away: the rename tool has to
    recognise what is ACTUALLY IN THE SPECS. Dropping this one early breaks
    renames for the 193 specs that still carry it.
    """
    # Arrange
    key = "SCITEX_TODO_AGENT_ID"
    # Act
    result = sub_env_value(key, OLD, OLD, NEW)
    # Assert
    assert result == NEW


def test_sub_env_value_rewrites_the_state_db_path_component():
    # Arrange
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    # Act
    result = sub_env_value(key, f"/state/{OLD}/state.db", OLD, NEW)
    # Assert
    assert result == f"/state/{NEW}/state.db"


def test_sub_env_value_ignores_an_env_var_that_is_not_an_identity():
    """``GIT_AUTHOR_NAME=scitex-todo`` would be a coincidence, not an identity."""
    # Arrange
    key = "GIT_AUTHOR_NAME"
    # Act
    result = sub_env_value(key, OLD, OLD, NEW)
    # Assert
    assert result == OLD


# ---------------------------------------------------------------------------
# Document-level rewrite — every touchpoint
# ---------------------------------------------------------------------------


def test_rewrite_updates_the_project_label(doc: dict):
    # Arrange
    labels = doc["metadata"]["labels"]
    # Act
    project = labels["project"]
    # Assert
    assert project == NEW


def test_rewrite_updates_the_purpose_label(doc: dict):
    # Arrange
    labels = doc["metadata"]["labels"]
    # Act
    purpose = labels["purpose"]
    # Assert
    assert purpose == f"{NEW}-maintainer"


def test_rewrite_updates_the_workdir(doc: dict):
    # Arrange
    spec = doc["spec"]
    # Act
    workdir = spec["workdir"]
    # Assert
    assert workdir == f"/home/tester/proj/{NEW}"


def test_rewrite_updates_the_overlay_path_inside_flat_raw_args(raw_args: list):
    """The overlay path is a bare list item AFTER ``--overlay``, not a key."""
    # Arrange
    idx = raw_args.index("--overlay") + 1
    # Act
    overlay = raw_args[idx]
    # Assert
    assert overlay == f"/home/tester/.scitex/agent-container/containers/overlays/{NEW}/"


def test_rewrite_updates_the_state_db_env(raw_args: list):
    # Arrange
    prefix = "SCITEX_AGENT_CONTAINER_STATE_DB="
    # Act
    entry = next(a for a in raw_args if a.startswith(prefix))
    # Assert
    assert entry == f"{prefix}/state/{NEW}/state.db"


def test_rewrite_updates_the_board_identity_env(raw_args: list):
    """THE one that orphans 158 cards if it moves without them."""
    # Arrange
    prefix = "SCITEX_TODO_AGENT_ID="
    # Act
    entry = next(a for a in raw_args if a.startswith(prefix))
    # Assert
    assert entry == f"{prefix}{NEW}"


def test_rewrite_leaves_an_unrelated_env_var_untouched(raw_args: list):
    # Arrange
    prefix = "GIT_AUTHOR_NAME="
    # Act
    entry = next(a for a in raw_args if a.startswith(prefix))
    # Assert
    assert entry == f"{prefix}Yusuke Watanabe"


def test_rewrite_updates_a_bind_that_carries_the_name():
    # Arrange
    spec = make_spec(OLD).replace(
        "      - /home/tester/.ssh:/home/agent/.ssh:ro",
        "      - /home/tester/proj/scitex-todo:/work:rw",
    )
    # Act
    text, _changes = rewrite_spec(spec, OLD, NEW)
    # Assert
    assert "/home/tester/proj/scitex-cards:/work:rw" in text


def test_rewrite_updates_the_equals_joined_overlay_spelling():
    """Both ``--overlay <p>`` and ``--overlay=<p>`` are live in the fleet."""
    # Arrange
    spec = make_spec(OLD).replace(
        "      - --overlay\n"
        "      - /home/tester/.scitex/agent-container/containers/overlays/"
        "scitex-todo/",
        "      - --overlay=/home/tester/.scitex/agent-container/containers/"
        "overlays/scitex-todo/",
    )
    # Act
    text, _changes = rewrite_spec(spec, OLD, NEW)
    # Assert
    assert (
        "--overlay=/home/tester/.scitex/agent-container/containers/overlays/"
        "scitex-cards/" in text
    )


# ---------------------------------------------------------------------------
# Corruption guards
# ---------------------------------------------------------------------------


def test_rewrite_preserves_the_operators_comments(rewritten: str):
    """Specs are ~40% load-bearing operator commentary. Destroying it is damage."""
    # Arrange
    marker = "# This comment block is LOAD-BEARING"
    # Act
    survived = marker in rewritten
    # Assert
    assert survived


def test_rewrite_preserves_the_comment_that_warns_about_the_board_identity(
    rewritten: str,
):
    # Arrange
    marker = "# every card the agent owns is orphaned."
    # Act
    survived = marker in rewritten
    # Assert
    assert survived


def test_rewrite_changes_no_key_it_did_not_plan_to_change(rewritten: str):
    """Every leaf that moved must be accounted for by a reported change."""
    # Arrange
    before = yaml.safe_load(make_spec(OLD))
    after = yaml.safe_load(rewritten)
    _text, changes = rewrite_spec(make_spec(OLD), OLD, NEW)
    # Act
    moved = _leaves(before) ^ _leaves(after)
    # Assert
    assert len(moved) == 2 * len(changes)


def test_rewrite_keeps_the_document_valid_yaml(rewritten: str):
    # Arrange
    expected_kind = "Agent"
    # Act
    doc = yaml.safe_load(rewritten)
    # Assert
    assert doc["kind"] == expected_kind


def test_rewrite_reports_every_change_it_made():
    # Arrange
    expected_paths = {
        "metadata.labels.project",
        "metadata.labels.purpose",
        "spec.workdir",
    }
    # Act
    _text, changes = rewrite_spec(make_spec(OLD), OLD, NEW)
    # Assert
    assert expected_paths <= {c.path for c in changes}


def test_rewrite_is_a_no_op_when_the_spec_never_names_the_agent():
    # Arrange
    spec = make_spec("someone-else")
    # Act
    text, changes = rewrite_spec(spec, OLD, NEW)
    # Assert
    assert (text, changes) == (spec, [])


def test_rewrite_refuses_a_spec_that_is_not_yaml():
    # Arrange
    text = "{{{ not yaml"
    # Act
    # Assert
    with pytest.raises(SpecRewriteError):
        rewrite_spec(text, OLD, NEW)


def test_rewrite_refuses_a_spec_that_is_not_a_mapping():
    # Arrange
    text = "- a\n- b\n"
    # Act
    # Assert
    with pytest.raises(SpecRewriteError):
        rewrite_spec(text, OLD, NEW)


def _leaves(node, prefix: str = "") -> set:
    """Flatten a doc to a set of ``(path, value)`` pairs."""
    out = set()
    if isinstance(node, dict):
        for key, value in node.items():
            out |= _leaves(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            out |= _leaves(value, f"{prefix}[{idx}]")
    else:
        out.add((prefix, node))
    return out
