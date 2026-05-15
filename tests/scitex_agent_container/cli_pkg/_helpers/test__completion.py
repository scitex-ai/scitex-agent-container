"""Tests for cli_pkg._helpers._completion — shell-completion callback.

No-mocks: uses real ``tmp_path`` directories with real ``spec.yaml``
fixture files, real environment variables (saved/restored via fixture),
and a real ``click.Context`` constructed from a real ``click.Command``.
No ``unittest.mock`` / ``MagicMock`` / ``monkeypatch`` / ``mocker``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import click
import pytest

from scitex_agent_container.cli_pkg._helpers._completion import (
    agent_name_complete,
)

_ENV_VAR = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
_HOME_KEYS = ("HOME", "SCITEX_AGENT_CONTAINER_YAML_DIRS")


# ---------------------------------------------------------------------------
# Real fixtures — write spec.yaml files; isolate env vars by save/restore.
# ---------------------------------------------------------------------------


def _write_spec(base: Path, name: str) -> None:
    """Materialize a minimal real ``spec.yaml`` under ``base/<name>/``."""
    agent_dir = base / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "spec.yaml").write_text("name: " + name + "\n")


@pytest.fixture
def isolated_env() -> Iterator[None]:
    """Save and restore HOME + the YAML dirs env var around the test."""
    saved = {k: os.environ.get(k) for k in _HOME_KEYS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def agents_dir(tmp_path: Path, isolated_env: None) -> Iterator[Path]:
    """Real agents-base dir wired in via $SCITEX_AGENT_CONTAINER_YAML_DIRS.

    HOME is redirected to an empty tmp dir so the primary search path
    (``~/.scitex/agent-container/agents``) does not leak real user state.
    """
    home = tmp_path / "home"
    home.mkdir()
    base = tmp_path / "agents"
    base.mkdir()
    os.environ["HOME"] = str(home)
    os.environ[_ENV_VAR] = str(base)
    # Move cwd off the real repo so project-local discovery
    # (.scitex/agent-container/agents walk-up) cannot leak in.
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield base
    finally:
        os.chdir(saved_cwd)


@pytest.fixture
def ctx_and_param() -> tuple[click.Context, click.Parameter]:
    """Real click.Context + click.Parameter — what Click hands the callback."""

    @click.command(name="dummy")
    @click.argument("name")
    def _cmd(name: str) -> None:  # pragma: no cover - not invoked
        pass

    param = _cmd.params[0]
    return click.Context(_cmd), param


# ---------------------------------------------------------------------------
# Tests — one assert each, AAA markers, 3+ word names.
# ---------------------------------------------------------------------------


def test_returns_all_when_incomplete_empty(
    agents_dir: Path, ctx_and_param: tuple[click.Context, click.Parameter]
) -> None:
    # Arrange
    for name in ("alpha", "beta", "gamma"):
        _write_spec(agents_dir, name)
    ctx, param = ctx_and_param
    # Act
    result = agent_name_complete(ctx, param, "")
    # Assert
    assert sorted(result) == ["alpha", "beta", "gamma"]


def test_filters_by_incomplete_prefix(
    agents_dir: Path, ctx_and_param: tuple[click.Context, click.Parameter]
) -> None:
    # Arrange
    for name in ("alpha", "alpine", "beta"):
        _write_spec(agents_dir, name)
    ctx, param = ctx_and_param
    # Act
    result = agent_name_complete(ctx, param, "alp")
    # Assert
    assert sorted(result) == ["alpha", "alpine"]


def test_returns_empty_on_no_match(
    agents_dir: Path, ctx_and_param: tuple[click.Context, click.Parameter]
) -> None:
    # Arrange
    _write_spec(agents_dir, "alpha")
    ctx, param = ctx_and_param
    # Act
    result = agent_name_complete(ctx, param, "zzz")
    # Assert
    assert result == []


def test_returns_empty_when_no_agents(
    agents_dir: Path, ctx_and_param: tuple[click.Context, click.Parameter]
) -> None:
    # Arrange — agents_dir exists but is empty; no spec.yaml files
    ctx, param = ctx_and_param
    # Act
    result = agent_name_complete(ctx, param, "")
    # Assert
    assert result == []


def test_ignores_dirs_without_spec(
    agents_dir: Path, ctx_and_param: tuple[click.Context, click.Parameter]
) -> None:
    # Arrange — directory exists but lacks spec.yaml; must not be listed
    (agents_dir / "ghost").mkdir()
    _write_spec(agents_dir, "real")
    ctx, param = ctx_and_param
    # Act
    result = agent_name_complete(ctx, param, "")
    # Assert
    assert result == ["real"]


def test_accepts_yml_spec_extension(
    agents_dir: Path, ctx_and_param: tuple[click.Context, click.Parameter]
) -> None:
    # Arrange — spec.yml (not .yaml) must also count
    agent_dir = agents_dir / "ymlagent"
    agent_dir.mkdir()
    (agent_dir / "spec.yml").write_text("name: ymlagent\n")
    ctx, param = ctx_and_param
    # Act
    result = agent_name_complete(ctx, param, "yml")
    # Assert
    assert result == ["ymlagent"]
