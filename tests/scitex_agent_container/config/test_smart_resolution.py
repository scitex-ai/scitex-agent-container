"""Tests for F-CS10 smart name resolution.

Covers ``enumerate_agent_names``, ``resolve_with_prefix``, and the
``AmbiguousAgent`` exception. Resolution must:

  1. Honour exact matches (delegating to ``resolve_config``).
  2. Fall back to prefix on miss; single hit → use it (with a stderr
     hint), multi hit → raise AmbiguousAgent listing the candidates,
     zero hits → re-raise FileNotFoundError so the existing 'Searched:'
     help renders.
  3. Path arguments (containing '/' or .yaml/.yml) bypass the entire
     fallback logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
