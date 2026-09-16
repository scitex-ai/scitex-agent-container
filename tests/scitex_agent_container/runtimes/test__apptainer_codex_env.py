"""Native Codex subscription auth is explicit, private, and fail-loud."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_codex_env import (
    preflight_subscription,
    sync_subscription_auth,
)


def test_selected_subscription_account_is_copied_to_private_codex_home(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    source = (
        tmp_path
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "openai"
        / "account-one"
        / "auth.json"
    )
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"auth_mode":"chatgpt"}')
    config = AgentConfig(name="native", harness="codex")
    config.subscription_provider = "openai"
    config.subscription_account = "openai:account-one"
    destination_home = tmp_path / "agent-codex-home"
    # Act
    destination = sync_subscription_auth(config, destination_home)
    # Assert
    assert (
        destination,
        destination.read_bytes() if destination else b"",
        os.stat(destination).st_mode & 0o777 if destination else 0,
    ) == (destination_home / "auth.json", b'{"auth_mode":"chatgpt"}', 0o600)


def test_preflight_executes_the_exact_declared_model_with_selected_auth(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    source = (
        tmp_path
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "openai"
        / "account-one"
        / "auth.json"
    )
    source.parent.mkdir(parents=True)
    source.write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    config = AgentConfig(name="native", harness="codex", workdir=str(tmp_path))
    config.claude.model = "gpt-5.6-sol"
    config.subscription_provider = "openai"
    config.subscription_account = "openai:account-one"
    seen: list[tuple[list[str], str]] = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["env"]["CODEX_HOME"]))
        return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

    # Act
    preflight_subscription(
        config,
        tmp_path / "state",
        which=lambda name: "/usr/bin/codex",
        run=run,
    )
    # Assert
    assert (seen[0][0][seen[0][0].index("-m") + 1], seen[0][1]) == (
        "gpt-5.6-sol",
        str(tmp_path / "state" / "codex-home"),
    )
