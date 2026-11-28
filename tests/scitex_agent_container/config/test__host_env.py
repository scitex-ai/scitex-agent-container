"""resolve_host_env: shell export > $HOME/.env, one var, nothing exported.

Real temporary ``$HOME`` with a real ``.env`` file; no mocks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.config._host_env import resolve_host_env

_KEYS = ("SAC_TEST_PROVIDER_KEY", "SAC_TEST_UNRELATED_SECRET")


@pytest.fixture
def fake_home(tmp_path: Path, env_save_restore) -> Path:
    """$HOME -> tmp dir whose .env holds the wanted key and an unrelated one."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "SAC_TEST_PROVIDER_KEY=from-home-dotenv\n"
        "export SAC_TEST_UNRELATED_SECRET='unrelated-value'\n"
    )
    env_save_restore.set("HOME", str(home))
    for key in _KEYS:
        env_save_restore.delete(key)
    return home


def test_value_comes_from_home_dotenv_when_unset(fake_home: Path) -> None:
    # Arrange
    name = "SAC_TEST_PROVIDER_KEY"
    # Act
    value = resolve_host_env(name)
    # Assert
    assert value == "from-home-dotenv"


def test_shell_export_wins_over_home_dotenv(fake_home: Path, env_save_restore) -> None:
    # Arrange
    env_save_restore.set("SAC_TEST_PROVIDER_KEY", "from-shell")
    # Act
    value = resolve_host_env("SAC_TEST_PROVIDER_KEY")
    # Assert
    assert value == "from-shell"


def test_missing_home_dotenv_resolves_to_empty(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("SAC_TEST_PROVIDER_KEY")
    # Act
    value = resolve_host_env("SAC_TEST_PROVIDER_KEY")
    # Assert
    assert value == ""


def test_lookup_exports_nothing_into_os_environ(fake_home: Path) -> None:
    # Arrange
    before = dict(os.environ)
    # Act
    resolve_host_env("SAC_TEST_PROVIDER_KEY")
    # Assert
    assert dict(os.environ) == before


# EOF
