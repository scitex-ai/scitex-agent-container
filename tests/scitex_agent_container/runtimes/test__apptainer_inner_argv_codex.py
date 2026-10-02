"""Tests for ``runtimes/_apptainer_inner_argv_codex`` — the codex TUI argv.

Real seams only: real ``AgentConfig`` / ``ClaudeSpec`` / ``ProviderSpec``
objects through the real builder. Each test pins one observable fact.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib

from scitex_agent_container._lifecycle._worktree_policy import (
    WorktreePlan,
    enforce_task_worktree_policy,
)
from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec
from scitex_agent_container.runtimes._apptainer_inner_argv_codex import (
    CODEX_EXEC_MODULE,
    CODEX_KEY_ENV,
    CODEX_PROVIDER_ID,
    codex_config_overrides,
    codex_tui_argv,
)
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError


def _config(**claude_kw) -> AgentConfig:
    claude = ClaudeSpec(
        model=claude_kw.pop("model", "qwen38-27b"),
        provider=claude_kw.pop(
            "provider",
            ProviderSpec(
                base_url="http://100.64.0.1:18772/",
                auth_token_env="FLEET_GATEWAY_KEY",
            ),
        ),
        **claude_kw,
    )
    return AgentConfig(
        name="hm", runtime="tui", workdir="/tmp/hm-wd", harness="codex", claude=claude
    )


def _overrides(flags: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(flags):
        if a == "-c" and i + 1 < len(flags):
            k, _, v = flags[i + 1].partition("=")
            out[k] = v
    return out


def test_overrides_select_the_sac_provider():
    # Arrange
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["model_provider"] == f'"{CODEX_PROVIDER_ID}"'


def test_overrides_point_the_provider_at_the_gateway_v1_root():
    # Arrange -- the spec's base_url is the Anthropic-style root; Codex wants /v1.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert (
        seen[f"model_providers.{CODEX_PROVIDER_ID}.base_url"]
        == '"http://100.64.0.1:18772/v1"'
    )


def test_overrides_speak_the_responses_api_only():
    # Arrange -- measured: "responses" is the only wire_api the binary accepts.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen[f"model_providers.{CODEX_PROVIDER_ID}.wire_api"] == '"responses"'


def test_overrides_name_the_key_env_sac_fills():
    # Arrange
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen[f"model_providers.{CODEX_PROVIDER_ID}.env_key"] == f'"{CODEX_KEY_ENV}"'


def test_overrides_carry_the_engine_model():
    # Arrange
    config = _config(model="qwen38-27b")
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["model"] == '"qwen38-27b"'


def test_overrides_disable_the_nested_sandbox():
    # Arrange -- bubblewrap cannot nest inside apptainer; the container is the boundary.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["sandbox_mode"] == '"danger-full-access"'


def test_overrides_carry_the_declared_context_window():
    # Arrange -- the engine fold leaves max_context_tokens on the config.
    config = _config()
    config.max_context_tokens = 1_048_576
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["model_context_window"] == "1048576"


def test_overrides_omit_the_context_window_when_none_is_declared():
    # Arrange
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert "model_context_window" not in seen


def test_overrides_refuse_a_config_without_a_provider():
    # Arrange -- silence would mean Codex's OpenAI-hosted default.
    config = _config(provider=None)
    # Act
    try:
        codex_config_overrides(config)
        message = ""
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "needs an inference provider" in message


def test_overrides_refuse_a_config_without_a_model():
    # Arrange
    config = _config(model="")
    config.model = ""
    # Act
    try:
        codex_config_overrides(config)
        message = ""
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "names no model" in message


def test_argv_runs_the_in_container_exec_shim():
    # Arrange -- the binary is resolved inside the container, not on the host.
    config = _config()
    # Act
    argv = codex_tui_argv(config)
    # Assert
    assert argv[:3] == ["python3", "-m", CODEX_EXEC_MODULE]


def test_argv_hands_the_mcp_files_to_the_shim():
    # Arrange
    config = _config()
    # Act
    argv = codex_tui_argv(
        config, mcp_config="/home/agent/.mcp.json", channel_mcp='{"a":1}'
    )
    # Assert
    assert argv[3:8] == [
        "--mcp-config",
        "/home/agent/.mcp.json",
        "--mcp-json",
        '{"a":1}',
        "--",
    ]


def test_argv_resumes_a_pinned_session_by_id():
    # Arrange -- spec.claude.session: resume + resume_id, like the Claude TUI.
    config = _config(session="resume", resume_id="0f4c1e6a-2b6d-4d0e-9c5b-7a1b2c3d4e5f")
    # Act
    argv = codex_tui_argv(config)
    # Assert
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == [
        "resume",
        "0f4c1e6a-2b6d-4d0e-9c5b-7a1b2c3d4e5f",
    ]


def test_argv_continues_the_latest_session_for_continue_mode():
    # Arrange
    config = _config(session="continue")
    # Act
    argv = codex_tui_argv(config)
    # Assert
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == ["resume", "--last"]


def test_argv_starts_fresh_without_a_session_directive():
    # Arrange
    config = _config()
    # Act
    argv = codex_tui_argv(config)
    # Assert -- the first thing after the separator is an override, not `resume`.
    assert argv[argv.index("--") + 1] == "-c"


def test_argv_hands_the_settings_to_the_shim_for_hooks():
    # Arrange -- the same settings path the Claude TUI launches with.
    config = _config()
    # Act
    argv = codex_tui_argv(config, settings="/home/agent/.claude/settings.json")
    # Assert
    assert argv[3:5] == ["--hooks-from", "/home/agent/.claude/settings.json"]


def test_overrides_trust_the_workdir_up_front():
    # Arrange -- otherwise Codex parks on its directory-trust picker at boot.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen['projects."/tmp/hm-wd".trust_level'] == '"trusted"'


def test_subscription_overrides_select_native_openai_and_exact_model():
    # Arrange
    config = _config(model="gpt-5.6-sol", provider=None)
    config.subscription_provider = "openai"
    config.subscription_account = "openai:person-example-com"
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert (seen["model_provider"], seen["model"], CODEX_PROVIDER_ID in seen) == (
        '"openai"',
        '"gpt-5.6-sol"',
        False,
    )


def _trusted_projects(config):
    flags = codex_config_overrides(config)
    return tomllib.loads("\n".join(flags[1::2]))["projects"]


@pytest.fixture
def approved_linked_worktree(tmp_path, env_save_restore):
    """Real Git, owner files, and the existing neutral-policy CLI boundary."""
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "runtime")
    )
    repo = tmp_path / 'repo "quoted"'
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "develop"], check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    fixture = (
        Path(__file__).parents[1]
        / "_lifecycle/_fixtures/worktree-policy/src/.bin/scitex-worktree-policy"
    )
    cli = tmp_path / "neutral-policy"
    shutil.copy2(fixture, cli)
    cli.chmod(0o700)
    config = _config()
    config.workdir = str(repo)
    enforce_task_worktree_policy(config, cli_path=cli)
    return config, repo


def test_overrides_trust_the_policy_verified_repository_root(approved_linked_worktree):
    # Arrange
    config, repo = approved_linked_worktree
    # Act
    projects = _trusted_projects(config)
    # Assert
    assert projects[str(repo)]["trust_level"] == "trusted"


def test_verified_repository_trust_keeps_the_selected_worktree(
    approved_linked_worktree,
):
    # Arrange
    config, _ = approved_linked_worktree
    # Act
    projects = _trusted_projects(config)
    # Assert
    assert projects[config.expanded_workdir]["trust_level"] == "trusted"


def test_verified_repository_trust_is_limited_to_the_root_and_worktree(
    approved_linked_worktree,
):
    # Arrange
    config, repo = approved_linked_worktree
    # Act
    projects = _trusted_projects(config)
    # Assert
    assert set(projects) == {str(repo), config.expanded_workdir}


def test_rendering_repository_trust_keeps_the_primary_branch(approved_linked_worktree):
    # Arrange
    config, repo = approved_linked_worktree
    # Act
    codex_config_overrides(config)
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Assert
    assert branch == "develop"


@pytest.mark.parametrize("plan_factory", [dict, SimpleNamespace])
def test_untyped_repository_hints_cannot_extend_trust(plan_factory):
    # Arrange
    config = _config()
    config._worktree_plan = plan_factory(
        repo_root="/unverified", resolved_workdir=config.workdir
    )
    # Act
    projects = _trusted_projects(config)
    # Assert
    assert set(projects) == {config.workdir}


def test_spec_like_repository_attributes_cannot_extend_trust():
    # Arrange
    config = _config()
    config.repo_root = "/unverified"
    config.trusted_projects = ["/"]
    # Act
    projects = _trusted_projects(config)
    # Assert
    assert set(projects) == {config.workdir}


def test_a_stale_internal_worktree_plan_cannot_extend_trust(approved_linked_worktree):
    # Arrange
    config, _ = approved_linked_worktree
    config.workdir = "/different/declared-workdir"
    # Act
    projects = _trusted_projects(config)
    # Assert
    assert set(projects) == {config.workdir}


@pytest.mark.parametrize(
    "root",
    [
        "/",
        "/tmp/..",
        "relative/repository",
        "/wildcard/*",
        "/wildcard/?",
        "/wildcard/[ab]",
    ],
)
def test_repository_trust_cannot_expand_to_global_or_pattern_paths(
    approved_linked_worktree, root
):
    # Arrange
    config, _ = approved_linked_worktree
    config._worktree_plan = replace(config._worktree_plan, repo_root=root)
    # Act
    projects = _trusted_projects(config)
    # Assert
    assert set(projects) == {config.workdir}


def test_workdir_and_repository_root_trust_are_not_duplicated():
    # Arrange
    config = _config()
    config._worktree_plan = WorktreePlan(
        authored_workdir=config.workdir,
        resolved_workdir=config.workdir,
        repo_root=config.workdir,
        branch="feature/example",
        action="reuse-explicit",
        owner_file="/fixture/runtime/worktree-owner.json",
    )
    # Act
    flags = codex_config_overrides(config)
    trust_entries = [
        value for value in flags if value.endswith('.trust_level="trusted"')
    ]
    # Assert
    assert len(trust_entries) == 1
