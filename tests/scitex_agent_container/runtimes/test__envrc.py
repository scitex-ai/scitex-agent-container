"""Evaluate ``to_home/.envrc`` into env vars for ``--env-file`` injection.

Covers ``_envrc.{eval_envrc, fold_envrc_into_env}``: a direnv-style ``.envrc``
is a shell script apptainer ``--env-file`` can't parse, so ``deploy_to_home``
evaluates it host-side (after the sibling ``.env``) and folds the net env into
the materialised ``.env``. Real bash + tmp files — no mocks (PA-306).
STX-TQ002 AAA-marker + STX-TQ007 one-assert.

Named ``test__envrc.py`` for the PS-204 §2 orphan-test mirror against
``src/scitex_agent_container/runtimes/_envrc.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.runtimes._envrc import (
    EnvrcEvalError,
    eval_envrc,
    fold_envrc_into_env,
)


def test_eval_envrc_captures_exported_var(tmp_path: Path) -> None:
    # Arrange
    envrc = tmp_path / ".envrc"
    envrc.write_text("export FIGRECIPE_FOO=bar\n", encoding="utf-8")
    # Act
    out = eval_envrc(envrc)
    # Assert
    assert out.get("FIGRECIPE_FOO") == "bar"


def test_eval_envrc_evaluates_command_substitution(tmp_path: Path) -> None:
    # Arrange
    envrc = tmp_path / ".envrc"
    envrc.write_text("export COMPUTED=$(printf abc)\n", encoding="utf-8")
    # Act
    out = eval_envrc(envrc)
    # Assert
    assert out.get("COMPUTED") == "abc"


def test_eval_envrc_sees_base_env_first(tmp_path: Path) -> None:
    # Arrange — .envrc references a var defined in the sibling .env.
    base = tmp_path / ".env"
    base.write_text("BASE_TOKEN=xyz\n", encoding="utf-8")
    envrc = tmp_path / ".envrc"
    envrc.write_text('export DERIVED="${BASE_TOKEN}-suffix"\n', encoding="utf-8")
    # Act
    out = eval_envrc(envrc, base_env=base)
    # Assert
    assert out.get("DERIVED") == "xyz-suffix"


def test_eval_envrc_raises_on_nonzero_exit(tmp_path: Path) -> None:
    # Arrange — a .envrc that fails (exit 1).
    envrc = tmp_path / ".envrc"
    envrc.write_text("echo boom >&2\nexit 1\n", encoding="utf-8")
    # Act
    # Assert
    with pytest.raises(EnvrcEvalError):
        eval_envrc(envrc)


def test_fold_envrc_writes_combined_env_file(tmp_path: Path) -> None:
    # Arrange — dest has both .env and .envrc; the .envrc adds a var.
    (tmp_path / ".env").write_text("FROM_ENV=1\n", encoding="utf-8")
    (tmp_path / ".envrc").write_text("export FROM_ENVRC=2\n", encoding="utf-8")
    # Act
    fold_envrc_into_env(tmp_path)
    # Assert — the folded .env carries BOTH sources.
    text = (tmp_path / ".env").read_text()
    assert "FROM_ENV=1" in text and "FROM_ENVRC=2" in text


def test_fold_envrc_noop_when_no_envrc(tmp_path: Path) -> None:
    # Arrange — only a .env, no .envrc.
    env_file = tmp_path / ".env"
    env_file.write_text("ONLY=env\n", encoding="utf-8")
    # Act
    fold_envrc_into_env(tmp_path)
    # Assert — .env left exactly as materialised.
    assert env_file.read_text() == "ONLY=env\n"


def test_fold_envrc_sets_owner_only_perms(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / ".envrc").write_text("export SECRET=s\n", encoding="utf-8")
    # Act
    fold_envrc_into_env(tmp_path)
    # Assert — the folded .env is chmod 0600.
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
