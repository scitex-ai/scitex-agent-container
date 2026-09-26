"""Hermes' profile env must reach the process before config imports."""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from scitex_agent_container.config._harness_callables import (
    _hermes_profile_env_argv,
)


def _write_profile_env(state_dir: Path, *, mode: int = 0o600) -> Path:
    path = state_dir / "home" / ".hermes" / ".env"
    path.parent.mkdir(parents=True)
    path.write_text("SAC_LISTEN_BEARER=test-secret\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_hermes_profile_env_is_passed_by_owner_only_file(tmp_path: Path) -> None:
    # Arrange
    path = _write_profile_env(tmp_path)

    # Act
    argv = _hermes_profile_env_argv(tmp_path)

    # Assert
    assert (argv, "test-secret" in " ".join(argv)) == (
        ["--env-file", str(path)],
        False,
    )


def test_hermes_profile_env_must_exist_before_argv_build(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path

    # Act
    action = partial(_hermes_profile_env_argv, state_dir)

    # Assert
    with pytest.raises(RuntimeError, match="materialize the Hermes workspace"):
        action()


def test_hermes_profile_env_refuses_group_or_world_access(tmp_path: Path) -> None:
    # Arrange
    _write_profile_env(tmp_path, mode=0o640)

    # Act
    action = partial(_hermes_profile_env_argv, tmp_path)

    # Assert
    with pytest.raises(RuntimeError, match="unsafe mode 0o640"):
        action()
