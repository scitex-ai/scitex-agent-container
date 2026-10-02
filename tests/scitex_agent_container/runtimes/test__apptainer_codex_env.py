"""Native Codex subscription auth is explicit, private, and fail-loud."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_codex_env import (
    _codex_sdk_routing_flags,
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


def test_headless_subscription_preserves_model_provider_and_reasoning_effort() -> None:
    # Arrange
    config = AgentConfig(name="native", harness="codex", runtime="headless")
    config.model = "gpt-6.1-sol"
    config.claude.model = "gpt-6.1-sol"
    config.subscription_provider = "openai"
    config.subscription_account = "openai:account-one"
    config.reasoning_effort = "xhigh"
    # Act
    flags = _codex_sdk_routing_flags(config)
    env = dict(value.split("=", 1) for value in flags[1::2])
    overrides = json.loads(base64.b64decode(env["SAC_CODEX_CONFIG_OVERRIDES_B64"]))
    # Assert
    assert (
        env["SAC_CODEX_MODEL"],
        env["SAC_CODEX_MODEL_PROVIDER"],
        'model_provider="openai"' in overrides,
        'model="gpt-6.1-sol"' in overrides,
        'model_reasoning_effort="xhigh"' in overrides,
    ) == ("gpt-6.1-sol", "openai", True, True, True)


def test_preflight_executes_the_exact_declared_model_with_selected_auth(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("CODEX_HOME", str(tmp_path / "state" / "codex-home"))
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


def test_preflight_fixture_preserves_an_inherited_external_auth_file(
    tmp_path: Path,
) -> None:
    # Arrange — a synthetic inherited profile stands in for the agent's own.
    external_home = tmp_path / "external-codex-home"
    external_home.mkdir()
    external_auth = external_home / "auth.json"
    sentinel = b'{"external_fixture":"preserve"}'
    external_auth.write_bytes(sentinel)
    child_env = dict(os.environ)
    child_env["CODEX_HOME"] = str(external_home)
    target = (
        f"{Path(__file__).resolve()}::"
        "test_preflight_executes_the_exact_declared_model_with_selected_auth"
    )
    # Act — exercise the real fixture in a child, never the current profile.
    subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header"],
        env=child_env, capture_output=True, check=True, timeout=5,
    )
    # Assert
    assert external_auth.read_bytes() == sentinel
