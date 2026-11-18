"""The per-agent token reaches the container through the 0600 env-file.

``agent_token_env_flags`` is what ``listen_env_flags`` appends beside
``SAC_LISTEN_BEARER``; ``redact_secret_env_to_file`` is the real secret lift
``finalize_flag_argv`` runs. Real files under ``tmp_path``, no mocks.
AAA-marked, one assert per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._listen.agent_tokens import (
    AGENT_TOKEN_ENV,
    resolve_agent_token,
)
from scitex_agent_container.runtimes._apptainer_listen_env import (
    agent_token_env_flags,
)
from scitex_agent_container.runtimes._apptainer_secret_env import (
    is_secret_env_key,
    redact_secret_env_to_file,
    secret_env_file_path,
)


def _lifted(tmp_path: Path) -> list[str]:
    state = tmp_path / "state"
    argv = ["exec", *agent_token_env_flags(state), "--env", "SAC_NAME=dev-agent"]
    return redact_secret_env_to_file(argv, state_dir=state)


def test_agent_token_env_name_is_secret_shaped():
    # Arrange
    key = AGENT_TOKEN_ENV
    # Act
    secret = is_secret_env_key(key)
    # Assert
    assert secret is True


def test_agent_token_never_remains_in_launcher_argv(tmp_path: Path):
    # Arrange
    _ = tmp_path
    # Act
    argv = _lifted(tmp_path)
    # Assert
    assert not [a for a in argv if AGENT_TOKEN_ENV in a]


def test_agent_token_is_written_to_the_secret_env_file(tmp_path: Path):
    # Arrange
    _lifted(tmp_path)
    # Act
    body = secret_env_file_path(tmp_path / "state").read_text(encoding="utf-8")
    # Assert
    assert f"{AGENT_TOKEN_ENV}=" in body


def test_agent_token_flags_need_a_state_dir():
    # Arrange
    state_dir = None
    # Act
    flags = agent_token_env_flags(state_dir)
    # Assert
    assert flags == []


def test_emitting_token_flags_does_not_activate_the_token(tmp_path: Path):
    # Arrange — what `sac agents explain` / `--dry-run` reach.
    flags = agent_token_env_flags(tmp_path / "state")
    token = flags[1].split("=", 1)[1]
    # Act
    got = resolve_agent_token(token, registry_dir=tmp_path / "registry")
    # Assert
    assert got is None
