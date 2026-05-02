"""Tests for resolve_config search order.

sac searches only its own state root plus the plugin-port env var — no
fallbacks to orochi or any other external orchestrator's paths.
"""

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


def _primary(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "agents"


def test_resolve_config_uses_primary_root(fake_home):
    hit = _write(_primary(fake_home) / "foo.yaml", "foo")
    assert resolve_config("foo") == str(hit)


def test_resolve_config_supports_nested_name_dir(fake_home):
    hit = _write(_primary(fake_home) / "foo" / "foo.yaml", "foo")
    assert resolve_config("foo") == str(hit)


def test_resolve_config_primary_preferred_over_env_var(fake_home, monkeypatch):
    primary_hit = _write(_primary(fake_home) / "foo.yaml", "primary")
    envdir = fake_home / "envdir"
    _write(envdir / "foo.yaml", "env")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(envdir))
    assert resolve_config("foo") == str(primary_hit)


def test_resolve_config_env_var_plugin_port(fake_home, monkeypatch):
    envdir = fake_home / "envdir"
    envhit = _write(envdir / "foo.yaml", "env")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(envdir))
    assert resolve_config("foo") == str(envhit)


def test_resolve_config_env_var_colon_separated(fake_home, monkeypatch):
    d1 = fake_home / "d1"
    d2 = fake_home / "d2"
    d1.mkdir()
    expected = _write(d2 / "foo.yaml", "d2")
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", f"{d1}:{d2}")
    assert resolve_config("foo") == str(expected)


def test_resolve_config_not_found_lists_searched_paths(fake_home, monkeypatch):
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
    assert "missing" in msg


def test_resolve_config_fleet_shared_agents_fallback(fake_home):
    """sac searches ~/.dotfiles/src/.scitex/orochi/shared/agents as a built-in
    fleet fallback (PR #99 / orochi-runtime-layout), so a yaml placed there
    IS resolved without setting SCITEX_AGENT_CONTAINER_YAML_DIRS."""
    orochi_path = (
        fake_home / ".dotfiles" / "src" / ".scitex" / "orochi" / "shared" / "agents"
    )
    yaml_file = _write(orochi_path / "foo" / "foo.yaml", "name: foo\nversion: 1\n")
    result = resolve_config("foo")
    assert result == str(yaml_file)


def test_resolve_config_absolute_path_unchanged(fake_home, tmp_path):
    abs_yaml = _write(tmp_path / "elsewhere" / "my.yaml", "abs")
    assert resolve_config(str(abs_yaml)) == str(abs_yaml)


def test_resolve_config_absolute_path_missing_raises(fake_home, tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_config(str(tmp_path / "nope" / "x.yaml"))
