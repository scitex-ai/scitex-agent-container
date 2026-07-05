"""Tests for the to_home/ text interpolators.

Focus: ``interpolate_env`` must NEVER bake per-agent IDENTITY vars at
deploy time — they stay as literal ``${VAR}`` placeholders for RUNTIME
expansion from the agent's own env (INCIDENT 2026-07-02, card
sac-mcp-json-per-agent-identity-not-ambient-env). Non-identity vars are
still substituted from ``os.environ`` (regression guard against
over-denylisting).

Also covers the 2026-07-05 follow-up: a per-project opt-in manifest
(``[tool.sac] runtime_only_env_vars`` in that project's OWN
``pyproject.toml``) that lets downstream packages declare their own
identity vars instead of sac hardcoding them (see
:func:`_load_project_runtime_only_vars`). The hardcoded
``_RUNTIME_ONLY_VARS`` set is kept as a deprecated fallback — both paths
must keep working.

STX-NM002: no mocks/monkeypatch — env mutated via the ``env_save_restore``
fixture (auto-reverts on teardown).
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._to_home_text import (
    _load_project_runtime_only_vars,
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


# _load_project_runtime_only_vars — per-project [tool.sac] manifest
def test_load_project_runtime_only_vars_reads_declared_names(tmp_path):
    # Arrange
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sac]\nruntime_only_env_vars = ["MYPKG_AGENT_ID", "MYPKG_TOKEN"]\n'
    )
    # Act
    out = _load_project_runtime_only_vars(str(tmp_path))
    # Assert
    assert out == frozenset({"MYPKG_AGENT_ID", "MYPKG_TOKEN"})


def test_load_project_runtime_only_vars_missing_pyproject_is_empty(tmp_path):
    # Arrange: no pyproject.toml written at all.
    # Act
    out = _load_project_runtime_only_vars(str(tmp_path))
    # Assert
    assert out == frozenset()


def test_load_project_runtime_only_vars_missing_tool_sac_table_is_empty(tmp_path):
    # Arrange
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "mypkg"\n')
    # Act
    out = _load_project_runtime_only_vars(str(tmp_path))
    # Assert
    assert out == frozenset()


def test_load_project_runtime_only_vars_malformed_toml_is_empty(tmp_path):
    # Arrange
    (tmp_path / "pyproject.toml").write_text("this is not [valid toml")
    # Act
    out = _load_project_runtime_only_vars(str(tmp_path))
    # Assert
    assert out == frozenset()


def test_load_project_runtime_only_vars_non_list_value_is_empty(tmp_path):
    # Arrange
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sac]\nruntime_only_env_vars = "not-a-list"\n'
    )
    # Act
    out = _load_project_runtime_only_vars(str(tmp_path))
    # Assert
    assert out == frozenset()


def test_load_project_runtime_only_vars_no_workdir_is_empty():
    # Arrange: no workdir at all (None).
    # Act
    out = _load_project_runtime_only_vars(None)
    # Assert
    assert out == frozenset()


# interpolate_env — per-project manifest vars stay literal (regression)
def test_interpolate_env_keeps_project_manifest_var_literal_even_when_set(
    tmp_path, env_save_restore
):
    # Arrange
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sac]\nruntime_only_env_vars = ["MYPKG_AGENT_ID"]\n'
    )
    config = AgentConfig(name="x", workdir=str(tmp_path))
    env_save_restore.set("MYPKG_AGENT_ID", "wrong-identity")
    # Act
    out = interpolate_env("agent=${MYPKG_AGENT_ID}", config)
    # Assert
    assert out == "agent=${MYPKG_AGENT_ID}"


def test_interpolate_env_still_honors_hardcoded_fallback_with_config(
    tmp_path, env_save_restore
):
    # No [tool.sac] manifest in this project — existing hardcoded
    # _RUNTIME_ONLY_VARS entries must still be protected (no regression).
    # Arrange
    config = AgentConfig(name="x", workdir=str(tmp_path))
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "wrong-identity")
    # Act
    out = interpolate_env("agent=${SCITEX_TODO_AGENT_ID}", config)
    # Assert
    assert out == "agent=${SCITEX_TODO_AGENT_ID}"


def test_interpolate_env_substitutes_non_identity_var_with_config(
    tmp_path, env_save_restore
):
    # A project manifest present but the var in question isn't in it —
    # still substitutes normally.
    # Arrange
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sac]\nruntime_only_env_vars = ["MYPKG_AGENT_ID"]\n'
    )
    config = AgentConfig(name="x", workdir=str(tmp_path))
    env_save_restore.set("SAC_TEST_NON_IDENTITY_VAR", "expanded-value")
    # Act
    out = interpolate_env("v=${SAC_TEST_NON_IDENTITY_VAR}", config)
    # Assert
    assert out == "v=expanded-value"
