"""Tests for the to_home/ text interpolators.

Focus: ``interpolate_env`` must NEVER bake per-agent IDENTITY vars at
deploy time — they stay as literal ``${VAR}`` placeholders for RUNTIME
expansion from the agent's own env (INCIDENT 2026-07-02, card
sac-mcp-json-per-agent-identity-not-ambient-env). Non-identity vars are
still substituted from ``os.environ`` (regression guard against
over-denylisting).

STX-NM002: no mocks/monkeypatch — env mutated via the ``env_save_restore``
fixture (auto-reverts on teardown).
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._to_home_text import (
    LEGACY_RENAMED_ENV_VARS,
    interpolate_env,
)


# interpolate_env — per-agent identity stays literal
def test_interpolate_env_keeps_cct_agent_id_literal_even_when_set(env_save_restore):
    # Arrange
    env_save_restore.set("CCT_AGENT_ID", "scitex-agent-container")
    # Act
    out = interpolate_env("id=${CCT_AGENT_ID}")
    # Assert
    assert out == "id=${CCT_AGENT_ID}"


def test_interpolate_env_keeps_scitex_todo_agent_id_literal_even_when_set(
    env_save_restore,
):
    # Arrange
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "agent:scitex-agent-container")
    # Act
    out = interpolate_env("agent=${SCITEX_TODO_AGENT_ID}")
    # Assert
    assert out == "agent=${SCITEX_TODO_AGENT_ID}"


def test_interpolate_env_keeps_legacy_scitex_todo_agent_literal_even_when_set(
    env_save_restore,
):
    # Legacy pre-0.7.30 name kept as a guard: a stale deployer shell must
    # still not bake it into materialized files.
    # Arrange
    env_save_restore.set("SCITEX_TODO_AGENT", "agent:scitex-agent-container")
    # Act
    out = interpolate_env("agent=${SCITEX_TODO_AGENT}")
    # Assert
    assert out == "agent=${SCITEX_TODO_AGENT}"


def test_interpolate_env_keeps_cct_bot_token_literal_even_when_set(env_save_restore):
    # Arrange
    env_save_restore.set("CCT_BOT_TOKEN", "123:wrong-token")
    # Act
    out = interpolate_env("token=${CCT_BOT_TOKEN}")
    # Assert
    assert out == "token=${CCT_BOT_TOKEN}"


# interpolate_env — non-identity vars still substitute (regression)
def test_interpolate_env_substitutes_non_identity_var(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_TEST_NON_IDENTITY_VAR", "expanded-value")
    # Act
    out = interpolate_env("v=${SAC_TEST_NON_IDENTITY_VAR}")
    # Assert
    assert out == "v=expanded-value"


def test_interpolate_env_leaves_unset_non_identity_var_literal(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_TEST_DEFINITELY_UNSET_VAR")
    # Act
    out = interpolate_env("v=${SAC_TEST_DEFINITELY_UNSET_VAR}")
    # Assert
    assert out == "v=${SAC_TEST_DEFINITELY_UNSET_VAR}"


# LEGACY_RENAMED_ENV_VARS — single source of truth shared with
# _apptainer_host_env.scrub_legacy_env and _apptainer_inner_argv's
# defense-in-depth unset step (INCIDENT 2026-07-05).
def test_legacy_renamed_env_vars_contains_scitex_todo_agent():
    # Arrange
    names = LEGACY_RENAMED_ENV_VARS
    # Act
    present = "SCITEX_TODO_AGENT" in names
    # Assert
    assert present


def test_legacy_renamed_env_vars_contains_scitex_todo_tasks():
    # Arrange
    names = LEGACY_RENAMED_ENV_VARS
    # Act
    present = "SCITEX_TODO_TASKS" in names
    # Assert
    assert present


def test_legacy_renamed_env_vars_is_exactly_two_names():
    # Arrange — future renames add here deliberately, not by accident
    # growing the set.
    names = LEGACY_RENAMED_ENV_VARS
    # Act
    expected = {"SCITEX_TODO_AGENT", "SCITEX_TODO_TASKS"}
    # Assert
    assert names == expected
