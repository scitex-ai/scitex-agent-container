"""An explicit engine request is never swallowed by a live-process no-op."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._start_engine_noop import (
    ExplicitEngineNoopError,
    assert_explicit_engine_noop_safe,
)
from scitex_agent_container.cli_pkg._health_engine import AGENT_ID_ENV
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_provider import ENGINE_KEY_ENV


def _config() -> AgentConfig:
    config = AgentConfig(name="hub", harness="hermes", runtime="tui")
    config.engine_key = "codex"
    return config


def _proc_root(tmp_path: Path, engine: str) -> Path:
    root = tmp_path / "proc"
    process = root / "4242"
    process.mkdir(parents=True)
    process.joinpath("environ").write_bytes(
        "\0".join(
            (
                f"{AGENT_ID_ENV}=hub",
                f"{ENGINE_KEY_ENV}={engine}",
            )
        ).encode("utf-8")
    )
    return root


def test_explicit_engine_refuses_noop_on_a_different_live_engine(
    tmp_path: Path,
) -> None:
    # Arrange
    config = _config()
    proc_root = _proc_root(tmp_path, "qwen38-27b")

    # Act
    def act() -> None:
        assert_explicit_engine_noop_safe(
            config,
            "codex",
            force=False,
            dry_run=False,
            proc_root=proc_root,
        )

    # Assert
    with pytest.raises(ExplicitEngineNoopError, match="--force --continue"):
        act()


def test_explicit_engine_allows_noop_when_the_live_engine_matches(
    tmp_path: Path,
) -> None:
    # Arrange
    config = _config()
    proc_root = _proc_root(tmp_path, "codex")

    # Act
    result = assert_explicit_engine_noop_safe(
        config,
        "codex",
        force=False,
        dry_run=False,
        proc_root=proc_root,
    )

    # Assert
    assert result is None
