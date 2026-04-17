"""Tests for resolve_config search order (todo#295)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import resolve_config


def _write(p: Path, name: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"metadata:\n  name: {name}\n")
    return p


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", raising=False)
    return tmp_path


def _legacy(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "agents"


def _dotfiles(home: Path) -> Path:
    return home / ".dotfiles" / "src" / ".scitex" / "orochi" / "shared" / "agents"


def test_resolve_config_prefers_legacy_path_over_dotfiles(fake_home):
    legacy = _write(_legacy(fake_home) / "foo.yaml", "legacy")
    _write(_dotfiles(fake_home) / "foo" / "foo.yaml", "dotfiles")
    assert resolve_config("foo") == str(legacy)


def test_resolve_config_falls_back_to_dotfiles(fake_home):
    expected = _write(_dotfiles(fake_home) / "foo" / "foo.yaml", "dotfiles")
    assert resolve_config("foo") == str(expected)


def test_resolve_config_env_var_takes_middle_priority(fake_home, monkeypatch):
    envdir = fake_home / "envdir"
    envhit = _write(envdir / "foo.yaml", "env")
    _write(_dotfiles(fake_home) / "foo" / "foo.yaml", "dotfiles")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(envdir))
    assert resolve_config("foo") == str(envhit)


def test_resolve_config_env_var_colon_separated(fake_home, monkeypatch):
    d1 = fake_home / "d1"
    d2 = fake_home / "d2"
    d1.mkdir()
    expected = _write(d2 / "foo.yaml", "d2")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", f"{d1}:{d2}")
    assert resolve_config("foo") == str(expected)


def test_resolve_config_not_found_lists_all_searched_paths(fake_home, monkeypatch):
    monkeypatch.setenv(
        "SCITEX_AGENT_CONTAINER_YAML_DIRS", f"{fake_home}/a:{fake_home}/b"
    )
    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_config("missing")
    msg = str(excinfo.value)
    assert ".scitex/agent-container/agents" in msg
    assert "SCITEX_AGENT_CONTAINER_YAML_DIRS" in msg
    assert f"{fake_home}/a" in msg
    assert f"{fake_home}/b" in msg
    assert ".dotfiles/src/.scitex/orochi/shared/agents" in msg
    assert "missing" in msg


def test_resolve_config_absolute_path_unchanged(fake_home, tmp_path):
    abs_yaml = _write(tmp_path / "elsewhere" / "my.yaml", "abs")
    assert resolve_config(str(abs_yaml)) == str(abs_yaml)


def test_resolve_config_absolute_path_missing_raises(fake_home, tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_config(str(tmp_path / "nope" / "x.yaml"))
