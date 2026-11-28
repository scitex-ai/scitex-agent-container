"""Mirror tests for ``secret_file_read_paths.py`` (normalisation + catalogue).

The asset tree is not an importable package (it deploys flat into
``$HOME/.claude/hooks/pre-tool-use/``), so the module is loaded by path.
"""

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
    "secret_file_read_paths", _DIR / "secret_file_read_paths.py"
)
paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(paths)


@pytest.fixture(autouse=True)
def _fake_home(tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    yield home
    if saved is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = saved


def test_expand_tilde_and_dotdot(_fake_home: Path) -> None:
    # Arrange
    raw = "~/work/../.ssh/./id_rsa"
    # Act
    expanded = paths.expand(raw, "/anywhere")
    # Assert
    assert expanded == f"{_fake_home}/.ssh/id_rsa"


def test_expand_dollar_home_braces(_fake_home: Path) -> None:
    # Arrange
    raw = "${HOME}/.netrc"
    # Act
    expanded = paths.expand(raw, "/anywhere")
    # Assert
    assert expanded == f"{_fake_home}/.netrc"


def test_relative_path_anchors_at_cwd() -> None:
    # Arrange
    raw = "../.ssh/id_rsa"
    # Act
    expanded = paths.expand(raw, "/home/u/work")
    # Assert
    assert expanded == "/home/u/.ssh/id_rsa"


def test_symlink_variant_resolves_to_target(_fake_home: Path) -> None:
    # Arrange
    (_fake_home / ".ssh").mkdir()
    key = _fake_home / ".ssh" / "id_ed25519"
    key.write_text("FAKE\n")
    link = _fake_home / "innocent"
    link.symlink_to(key)
    # Act
    found = paths.variants(str(link), "/")
    # Assert
    assert str(key) in found


def test_brace_expansion_yields_each_alternative() -> None:
    # Arrange
    raw = "/h/.s{sh,x}/id_rsa"
    # Act
    found = set(paths.variants(raw, "/"))
    # Assert
    assert {"/h/.ssh/id_rsa", "/h/.sx/id_rsa"} <= found


@pytest.mark.parametrize(
    "path",
    [
        "/h/.ssh/id_rsa",
        "/h/.ssh/id_ed25519",
        "/etc/ssh/ssh_host_rsa_key",
        "/h/.pw/anything",
        "/h/.gnupg/pubring.kbx",
        "/var/lib/scitex-secrets-signing/key",
        "/h/.bash.d/secrets/access_tokens/x",
        "/h/.bash.d/secrets/a.gitea-token",
        "/h/.bash.d/secrets/a.a2a-token",
        "/srv/key.pfx",
        "/h/.git-credentials",
        "/h/_netrc",
        "/h/.aws/credentials",
    ],
)
def test_secret_files_classify(path: str) -> None:
    # Arrange
    target = path
    # Act
    label = paths.classify(target, recursive=False)
    # Assert
    assert label is not None


@pytest.mark.parametrize(
    "path",
    [
        "/h/.ssh/id_rsa.pub",
        "/h/.ssh/known_hosts",
        "/h/.ssh",
        "/h/.pw",
        "/h/.bash.d/secrets",
        "/h/.bash.d/secrets/README.md",
        "/etc/ssl/certs/x.pem",
        "/h/project/keys.md",
        "/h/.aws/config",
    ],
)
def test_non_secret_or_bare_directory_does_not_classify(path: str) -> None:
    # Arrange
    target = path
    # Act
    label = paths.classify(target, recursive=False)
    # Assert
    assert label is None


@pytest.mark.parametrize(
    "path", ["/h/.ssh", "/h/.pw", "/h/.gnupg", "/h/.bash.d/secrets", "/h/.aws"]
)
def test_secret_directories_count_for_recursive_readers(path: str) -> None:
    # Arrange
    target = path
    # Act
    label = paths.classify(target, recursive=True)
    # Assert
    assert label is not None


def test_wildcard_that_can_expand_to_key_is_secret() -> None:
    # Arrange
    (found,) = paths.variants("/h/.ssh/*", "/")
    # Act
    label = paths.classify(found, recursive=False)
    # Assert
    assert label == "SSH private key"


def test_wildcard_limited_to_public_keys_is_not_secret() -> None:
    # Arrange
    (found,) = paths.variants("/h/.ssh/*.pub", "/")
    # Act
    label = paths.classify(found, recursive=False)
    # Assert
    assert label is None


def test_broad_wildcard_outside_secret_dirs_is_not_secret() -> None:
    # Arrange
    (found,) = paths.variants("/h/**/*", "/")
    # Act
    label = paths.classify(found, recursive=False)
    # Assert
    assert label is None
