"""Mirror tests for ``secret_file_read_bash.py`` (Bash read detection)."""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "secret_read_hooks"
)
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location(
    "secret_file_read_bash", _DIR / "secret_file_read_bash.py"
)
bash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bash)


@pytest.fixture(autouse=True)
def _fake_home(tmp_path: Path) -> Iterator[None]:
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path / "home")
    yield
    if saved is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = saved


def test_program_words_see_through_wrappers() -> None:
    # Arrange
    words = ["FOO=1", "timeout", "5", "nice", "-n", "10", "cat", "x"]
    # Act
    progs, start = bash._program_words(words)
    # Assert
    assert (progs, start) == (["timeout", "nice", "cat"], 1)


def test_program_words_see_find_exec() -> None:
    # Arrange
    words = ["find", ".", "-exec", "cat", "{}", "+"]
    # Act
    progs, _ = bash._program_words(words)
    # Assert
    assert progs == ["find", "cat"]


def test_redirect_feeds_any_program() -> None:
    # Arrange
    command = "my-tool --flag < ~/.netrc"
    # Act
    hits = bash.hits_for_bash(command, "/")
    # Assert
    assert [h.path for h in hits] == ["~/.netrc"]


def test_cd_moves_anchor_for_later_relative_paths() -> None:
    # Arrange
    command = "cd ~/.gnupg; cd private-keys-v1.d && cat ABC.key"
    # Act
    hits = bash.hits_for_bash(command, "/")
    # Assert
    assert len(hits) == 1


def test_nested_shell_string_is_parsed() -> None:
    # Arrange
    command = "sh -c \"bash -c 'head ~/.ssh/id_rsa'\""
    # Act
    hits = bash.hits_for_bash(command, "/")
    # Assert
    assert len(hits) == 1


def test_non_reader_mentioning_secret_path_is_allowed() -> None:
    # Arrange
    command = "ls -l ~/.ssh/id_rsa && chmod 600 ~/.ssh/id_rsa"
    # Act
    hits = bash.hits_for_bash(command, "/")
    # Assert
    assert hits == []


def test_gpg_homedir_value_is_configuration_not_a_read() -> None:
    # Arrange
    command = "gpg --homedir ~/.gnupg --list-keys"
    # Act
    hits = bash.hits_for_bash(command, "/")
    # Assert
    assert hits == []


def test_scp_remote_path_is_judged() -> None:
    # Arrange
    command = "scp host:.ssh/id_rsa /tmp/"
    # Act
    hits = bash.hits_for_bash(command, "/home/u")
    # Assert
    assert len(hits) == 1


def test_unbalanced_quote_fails_open() -> None:
    # Arrange
    command = "cat '~/.ssh/id_rsa"
    # Act
    hits = bash.hits_for_bash(command, "/")
    # Assert
    assert hits == []
