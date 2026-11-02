"""Use real Git objects behind the release API's explicit query seam."""

import base64
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sac_release_guard", ROOT / ".github/ci/release-identity.py")
IDENTITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IDENTITY)


@pytest.fixture()
def source_packet(tmp_path):
    env = {"PATH": os.defpath, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], env=env).decode().strip()
    git("init", "--initial-branch=develop", "-q")
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    git("add", "pyproject.toml")
    git("-c", "user.name=Release fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "source fixture")
    commit = git("rev-parse", "HEAD")
    version = IDENTITY.tomllib.loads((tmp_path / "pyproject.toml").read_text())["project"]["version"]
    tag = "v" + version
    git("tag", tag)
    git("branch", "main")
    main = {"name": "main", "protected": True, "commit": {"sha": commit}}
    def query(path):
        if "/git/ref/tags/" in path:
            return {"ref": "refs/tags/" + tag, "object": {"type": "commit", "sha": git("rev-parse", tag)}}
        if "/compare/" in path:
            head = git("rev-parse", "develop")
            return {"base_commit": {"sha": commit}, "status": "identical" if head == commit else "ahead"}
        if path.endswith("/branches/main"):
            return main
        if "/contents/pyproject.toml?ref=" in path:
            raw = subprocess.check_output(["git", "-C", str(tmp_path), "show", commit + ":pyproject.toml"], env=env)
            return {"encoding": "base64", "content": base64.b64encode(raw).decode(), "sha": git("rev-parse", commit + ":pyproject.toml")}
        raise AssertionError("Unexpected identity request: " + path)
    return tag, commit, main, query


def test_real_source_tag_on_protected_main_is_accepted(source_packet):
    # Arrange
    tag, commit, _main, query = source_packet
    # Act
    result = IDENTITY.resolve(tag, query)
    # Assert
    assert result == {"tag": tag, "commit": commit, "version": tag[1:]}


@pytest.mark.parametrize("field,value", [("protected", False), ("protected", 1), ("name", "develop"), ("commit", {"sha": "a" * 40})])
def test_main_identity_or_protection_changes_refuse(source_packet, field, value):
    # Arrange
    tag, _commit, main, query = source_packet
    main[field] = value
    # Act
    # Assert
    with pytest.raises(ValueError, match="protected main"):
        IDENTITY.resolve(tag, query)


def test_develop_ancestry_requirement_is_retained(source_packet):
    # Arrange
    tag, _commit, _main, query = source_packet
    def diverged(path):
        return {"base_commit": {"sha": "b" * 40}, "status": "diverged"} if "/compare/" in path else query(path)
    # Act
    # Assert
    with pytest.raises(ValueError, match="accepted develop"):
        IDENTITY.resolve(tag, diverged)
