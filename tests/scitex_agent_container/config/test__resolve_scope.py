"""Tests for ``$SAC_AGENT_SCOPE`` — fail loud on ambiguous registry scope.

The resolver checked the project-local registry FIRST, so a fleet-management
op run from inside a repo that ships its own ``.scitex/agent-container/agents/``
would SILENTLY resolve the project-local registry and never see the user-scope
fleet (the "sac-from-sac" breakage). The fix: when BOTH registry dirs exist and
``$SAC_AGENT_SCOPE`` is unset → raise ``AmbiguousRegistryScope``. Explicit
``user`` / ``project`` disambiguate; a single present registry resolves
silently (CI-safe — a fresh runner has no fleet dir).

No mocks: env vars go through a snapshot/restore bag, cwd through a real
``os.chdir`` bag, and project-local discovery is driven by chdir-ing into a
real git repo on the filesystem (``_make_project_repo``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import resolve_config
from scitex_agent_container.config._resolve import (
    AmbiguousRegistryScope,
    enumerate_agent_names,
)


class _EnvBag:
    """Mutable env wrapper that snapshots the prior value of every key it
    touches and reverts on ``restore()``. PA-306-friendly replacement for
    ``monkeypatch.setenv`` / ``monkeypatch.delenv``."""

    def __init__(self) -> None:
        self._prev: dict[str, "str | None"] = {}

    def setenv(self, key: str, value: str) -> None:
        import os

        if key not in self._prev:
            self._prev[key] = os.environ.get(key)
        os.environ[key] = value

    def delenv(self, key: str) -> None:
        import os

        if key not in self._prev:
            self._prev[key] = os.environ.get(key)
        os.environ.pop(key, None)

    def restore(self) -> None:
        import os

        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Cwd:
    """Real ``os.chdir`` wrapper that snapshots the prior cwd and reverts on
    ``restore()``. No-mock replacement for monkeypatch.chdir: it moves the
    *actual* process cwd so production's ``Path.cwd()`` walk-up sees real
    on-disk state."""

    def __init__(self) -> None:
        import os

        self._prev = os.getcwd()

    def chdir(self, path: "Path") -> None:
        import os

        os.chdir(str(path))

    def restore(self) -> None:
        import os

        os.chdir(self._prev)


@pytest.fixture
def env_bag():
    """Yields an ``_EnvBag`` that auto-reverts on teardown."""
    bag = _EnvBag()
    try:
        yield bag
    finally:
        bag.restore()


@pytest.fixture
def cwd_bag():
    """Yields a ``_Cwd`` that auto-reverts the real process cwd."""
    bag = _Cwd()
    try:
        yield bag
    finally:
        bag.restore()


def _write(p: Path, name: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"metadata:\n  name: {name}\n")
    return p


def _primary(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "agents"


def _make_project_repo(root: Path) -> Path:
    """Create a real git-repo project at ``root`` with a
    ``.scitex/agent-container/agents`` registry and return that agents
    dir. A bare ``.git`` marker dir is enough for the native project-scope
    walk-up (it only checks ``.git`` exists)."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    agents = root / ".scitex" / "agent-container" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    return agents


@pytest.fixture
def both_registries(tmp_path, env_bag, cwd_bag):
    """Stand up BOTH a fleet (``$HOME/.scitex/...``) and a project-local
    registry on the REAL filesystem, each with a same-named ``dup`` agent
    pointing at a distinct spec so the resolved path reveals which scope
    won. Drives project-local discovery by chdir-ing into a real git
    repo — no monkeypatch of production internals.

    ``$SAC_AGENT_SCOPE`` is cleared up-front; individual tests set it.
    """
    home = tmp_path / "home"
    fleet = home / ".scitex" / "agent-container" / "agents"
    fleet_hit = _write(fleet / "dup" / "spec.yaml", "fleet")
    project = _make_project_repo(tmp_path / "repo")
    project_hit = _write(project / "dup" / "spec.yaml", "project")
    env_bag.setenv("HOME", str(home))
    env_bag.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    env_bag.delenv("SAC_AGENT_SCOPE")
    cwd_bag.chdir(tmp_path / "repo")
    return {
        "fleet": fleet,
        "project": project,
        "fleet_hit": fleet_hit,
        "project_hit": project_hit,
    }


def test_unset_scope_with_both_registries_raises_ambiguous(both_registries):
    # Arrange
    target = "dup"
    # Act
    raises_ctx = pytest.raises(AmbiguousRegistryScope)
    # Assert
    with raises_ctx:
        resolve_config(target)


@pytest.fixture
def ambiguous_scope_message(both_registries):
    """Trigger ``AmbiguousRegistryScope`` and return (message, paths)."""
    # Arrange
    with pytest.raises(AmbiguousRegistryScope) as exc:
        resolve_config("dup")
    # Act
    return str(exc.value), both_registries


@pytest.mark.parametrize("getter", ["project", "fleet"])
def test_ambiguous_error_names_both_absolute_paths(ambiguous_scope_message, getter):
    # Arrange
    msg, paths = ambiguous_scope_message
    # Act
    contained = str(paths[getter]) in msg
    # Assert
    assert contained


def test_ambiguous_error_message_names_user_scope_fix(ambiguous_scope_message):
    # Arrange
    msg, _paths = ambiguous_scope_message
    # Act
    contained = "SAC_AGENT_SCOPE=user" in msg
    # Assert
    assert contained


def test_ambiguous_error_message_names_project_scope_fix(ambiguous_scope_message):
    # Arrange
    msg, _paths = ambiguous_scope_message
    # Act
    contained = "SAC_AGENT_SCOPE=project" in msg
    # Assert
    assert contained


def test_scope_user_resolves_fleet_and_ignores_project_local(
    both_registries, env_bag
):
    # Arrange
    env_bag.setenv("SAC_AGENT_SCOPE", "user")
    # Act
    result = resolve_config("dup")
    # Assert
    assert result == str(both_registries["fleet_hit"])


def test_scope_project_resolves_project_local(both_registries, env_bag):
    # Arrange
    env_bag.setenv("SAC_AGENT_SCOPE", "project")
    # Act
    result = resolve_config("dup")
    # Assert
    assert result == str(both_registries["project_hit"])


def test_scope_value_is_case_insensitive_and_trimmed(both_registries, env_bag):
    # Arrange
    env_bag.setenv("SAC_AGENT_SCOPE", "  User  ")
    # Act
    result = resolve_config("dup")
    # Assert
    assert result == str(both_registries["fleet_hit"])


def test_unknown_scope_value_treated_as_unset_and_raises(both_registries, env_bag):
    # Arrange — a typo must NOT silently pick a scope; it stays "unset".
    env_bag.setenv("SAC_AGENT_SCOPE", "fleet-typo")
    # Act
    raises_ctx = pytest.raises(AmbiguousRegistryScope)
    # Assert
    with raises_ctx:
        resolve_config("dup")


def test_enumerate_agent_names_raises_on_ambiguity(both_registries):
    # Arrange — same rule applies to enumeration, not just resolution.
    raises_ctx = pytest.raises(AmbiguousRegistryScope)
    # Act
    # Assert
    with raises_ctx:
        enumerate_agent_names()


def test_enumerate_agent_names_scope_user_lists_only_fleet(
    both_registries, env_bag
):
    # Arrange
    fleet_root = both_registries["fleet"]
    (fleet_root / "fleet-only").mkdir()
    (fleet_root / "fleet-only" / "spec.yaml").write_text("metadata:\n  name: x\n")
    env_bag.setenv("SAC_AGENT_SCOPE", "user")
    # Act
    names = enumerate_agent_names()
    # Assert
    assert "fleet-only" in names


def test_only_project_local_present_resolves_with_no_error(
    tmp_path, env_bag, cwd_bag
):
    """CI-safe path: a fresh runner has no ``~/.scitex/.../agents`` fleet
    dir, so ONLY project-local exists → resolve it silently, no error."""
    # Arrange
    home = tmp_path / "home"  # note: NO fleet dir created under it
    project = _make_project_repo(tmp_path / "repo")
    hit = _write(project / "solo" / "spec.yaml", "project")
    env_bag.setenv("HOME", str(home))
    env_bag.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    env_bag.delenv("SAC_AGENT_SCOPE")
    cwd_bag.chdir(tmp_path / "repo")
    # Act
    result = resolve_config("solo")
    # Assert
    assert result == str(hit)


def test_only_fleet_present_resolves_with_no_error(tmp_path, env_bag, cwd_bag):
    """Only the fleet registry exists (no project-local) → resolve it,
    no ambiguity. We chdir into a non-project tmp dir so the project-local
    walk-up finds nothing."""
    # Arrange
    home = tmp_path / "home"
    env_bag.setenv("HOME", str(home))
    env_bag.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    env_bag.delenv("SAC_AGENT_SCOPE")
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    cwd_bag.chdir(plain)
    hit = _write(_primary(home) / "solo" / "spec.yaml", "fleet")
    # Act
    result = resolve_config("solo")
    # Assert
    assert result == str(hit)
