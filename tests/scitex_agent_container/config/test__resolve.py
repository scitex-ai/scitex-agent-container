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


# ---------------------------------------------------------------------------
# F-CS10 — smart name resolution
#
# Covers ``enumerate_agent_names``, ``resolve_with_prefix``, and the
# ``AmbiguousAgent`` exception. Resolution must:
#   1. Honour exact matches (delegating to ``resolve_config``).
#   2. Fall back to prefix on miss; single hit → use it (with a stderr
#      hint), multi hit → raise AmbiguousAgent listing the candidates,
#      zero hits → re-raise FileNotFoundError.
#   3. Path arguments (containing '/' or .yaml/.yml) bypass the entire
#      fallback logic.
# ---------------------------------------------------------------------------

from scitex_agent_container.config._resolve import (
    AmbiguousAgent,
    enumerate_agent_names,
    resolve_with_prefix,
)


@pytest.fixture
def agent_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox a fresh agent search root for each test.

    ``_resolve._search_dirs()`` resolves the home root via
    ``os.path.expanduser('~')`` which honours ``$HOME`` on Unix —
    pointing $HOME at a tmp dir redirects every default location
    into our sandbox without touching any pathlib internals.
    """
    home = tmp_path / "home"
    (home / ".scitex" / "agent-container" / "agents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Don't let an outer test session leak its YAML dirs in.
    monkeypatch.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", raising=False)
    return home / ".scitex" / "agent-container" / "agents"


def _mkagent(root: Path, name: str) -> None:
    """Create a minimal valid <root>/<name>/<name>.yaml fixture."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec: { runtime: docker }\n"
    )


def test_enumerate_returns_all_agents(agent_root: Path):
    _mkagent(agent_root, "alpha")
    _mkagent(agent_root, "beta")
    _mkagent(agent_root, "gamma")
    names = enumerate_agent_names()
    assert {"alpha", "beta", "gamma"}.issubset(set(names))


def test_resolve_with_prefix_exact_match(agent_root: Path):
    _mkagent(agent_root, "alpha")
    p = resolve_with_prefix("alpha")
    assert p.endswith("/alpha/alpha.yaml")


def test_resolve_with_prefix_unique_prefix_resolves(agent_root: Path, capsys):
    _mkagent(agent_root, "polish-clew")
    p = resolve_with_prefix("polish-")
    assert p.endswith("/polish-clew/polish-clew.yaml")
    err = capsys.readouterr().err
    assert "polish-clew" in err
    assert "prefix match" in err


def test_resolve_with_prefix_ambiguous_raises(agent_root: Path):
    _mkagent(agent_root, "sai-factorout")
    _mkagent(agent_root, "sai-test")
    _mkagent(agent_root, "sai-test2")
    with pytest.raises(AmbiguousAgent) as exc:
        resolve_with_prefix("sai")
    assert exc.value.prefix == "sai"
    assert sorted(exc.value.matches) == [
        "sai-factorout",
        "sai-test",
        "sai-test2",
    ]


def test_resolve_with_prefix_no_match_raises_file_not_found(agent_root: Path):
    with pytest.raises(FileNotFoundError):
        resolve_with_prefix("ghost-agent-no-prefix-hits")


def test_path_argument_bypasses_smart_logic(agent_root: Path, tmp_path: Path):
    """A path argument must NOT trigger prefix matching — the user
    provided an explicit file path, honour it."""
    yaml_path = tmp_path / "explicit.yaml"
    yaml_path.write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec: { runtime: docker }\n"
    )
    assert resolve_with_prefix(str(yaml_path)) == str(yaml_path)


def test_ambiguous_agent_str_lists_matches():
    err = AmbiguousAgent("sai", ["sai-test", "sai-factorout"])
    s = str(err)
    assert "sai" in s
    assert "sai-test" in s
    assert "sai-factorout" in s
