"""Tests for resolve_config search order.

sac searches only its own state root plus the plugin-port env var — no
fallbacks to any external orchestrator's paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import resolve_config
from scitex_agent_container.config._resolve import (
    AmbiguousAgent,
    enumerate_agent_names,
    resolve_with_prefix,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml


def _write(p: Path, name: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"metadata:\n  name: {name}\n")
    return p


class _EnvBag:
    """Mutable env wrapper that snapshots the prior value of every key
    it touches and reverts on ``restore()``. PA-306-friendly replacement
    for ``monkeypatch.setenv`` / ``monkeypatch.delenv``."""

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


@pytest.fixture
def env_bag():
    """Yields an ``_EnvBag`` that auto-reverts on teardown."""
    bag = _EnvBag()
    try:
        yield bag
    finally:
        bag.restore()


@pytest.fixture(autouse=True)
def _neutral_cwd(tmp_path):
    """Run every test in this module from a project-less tmp cwd.

    The test process's real cwd is the sac worktree, which ships a
    tracked ``.scitex/agent-container/agents/`` project-local registry.
    With the new ``$SAC_AGENT_SCOPE`` ambiguity rule, that project-local
    dir + a test's tmp ``$HOME`` fleet dir would otherwise trip
    ``AmbiguousRegistryScope`` in tests that only mean to exercise the
    fleet path. Chdir-ing to a fresh tmp dir (no ``.git`` / ``.scitex``)
    makes project-scope discovery find nothing, preserving each test's
    single-registry intent. Reverts the real cwd on teardown.
    """
    import os

    prev = os.getcwd()
    neutral = tmp_path / "_neutral_cwd"
    neutral.mkdir()
    os.chdir(str(neutral))
    try:
        yield
    finally:
        os.chdir(prev)


@pytest.fixture
def fake_home(tmp_path, env_bag):
    """Redirect ``$HOME`` at tmp_path. Reverts via the env_bag."""
    env_bag.setenv("HOME", str(tmp_path))
    env_bag.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    env_bag.delenv("SAC_AGENT_SCOPE")
    return tmp_path


def _primary(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "agents"


def test_resolve_config_finds_spec_in_primary_agents_root(fake_home):
    # Arrange
    hit = _write(_primary(fake_home) / "foo" / "spec.yaml", "foo")
    # Act
    result = resolve_config("foo")
    # Assert
    assert result == str(hit)


def test_resolve_config_finds_spec_in_nested_name_directory(fake_home):
    """Every agent must live in its own directory with a ``spec.yaml``."""
    # Arrange
    hit = _write(_primary(fake_home) / "foo" / "spec.yaml", "foo")
    # Act
    result = resolve_config("foo")
    # Assert
    assert result == str(hit)


def test_resolve_config_prefers_primary_root_over_env_var_dir(fake_home, env_bag):
    # Arrange
    primary_hit = _write(_primary(fake_home) / "foo" / "spec.yaml", "primary")
    envdir = fake_home / "envdir"
    _write(envdir / "foo" / "spec.yaml", "env")
    env_bag.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(envdir))
    # Act
    result = resolve_config("foo")
    # Assert
    assert result == str(primary_hit)


def test_resolve_config_falls_back_to_env_var_plugin_port(fake_home, env_bag):
    # Arrange
    envdir = fake_home / "envdir"
    envhit = _write(envdir / "foo" / "spec.yaml", "env")
    env_bag.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(envdir))
    # Act
    result = resolve_config("foo")
    # Assert
    assert result == str(envhit)


def test_resolve_config_walks_colon_separated_env_var_dirs(fake_home, env_bag):
    # Arrange
    d1 = fake_home / "d1"
    d2 = fake_home / "d2"
    d1.mkdir()
    expected = _write(d2 / "foo" / "spec.yaml", "d2")
    env_bag.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", f"{d1}:{d2}")
    # Act
    result = resolve_config("foo")
    # Assert
    assert result == str(expected)


@pytest.fixture
def missing_config_error(fake_home, env_bag):
    """Trigger ``resolve_config('missing')`` and return the error message."""
    # Arrange
    env_bag.setenv(
        "SCITEX_AGENT_CONTAINER_YAML_DIRS",
        f"{fake_home}/a:{fake_home}/b",
    )
    # Act
    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_config("missing")
    return str(excinfo.value), fake_home


@pytest.mark.parametrize(
    "fragment_template",
    [
        ".scitex/agent-container/agents",
        "SCITEX_AGENT_CONTAINER_YAML_DIRS",
        "{home}/a",
        "{home}/b",
        "missing",
    ],
)
def test_resolve_config_not_found_error_lists_searched_path(
    missing_config_error, fragment_template
):
    # Arrange
    msg, fake_home = missing_config_error
    fragment = fragment_template.format(home=fake_home)
    # Act
    contained = fragment in msg
    # Assert
    assert contained


def test_resolve_config_returns_absolute_path_unchanged_when_file_exists(
    fake_home, tmp_path
):
    # Arrange
    abs_yaml = _write(tmp_path / "elsewhere" / "my.yaml", "abs")
    # Act
    result = resolve_config(str(abs_yaml))
    # Assert
    assert result == str(abs_yaml)


def test_resolve_config_absolute_path_raises_when_file_missing(fake_home, tmp_path):
    # Arrange
    missing_abs = str(tmp_path / "nope" / "x.yaml")
    # Act
    raises_ctx = pytest.raises(FileNotFoundError)
    # Assert
    with raises_ctx:
        resolve_config(missing_abs)


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


@pytest.fixture
def agent_root(tmp_path: Path, env_bag):
    """Sandbox a fresh agent search root for each test.

    ``_resolve._search_dirs()`` resolves the home root via
    ``os.path.expanduser('~')`` which honours ``$HOME`` on Unix —
    pointing $HOME at a tmp dir redirects every default location
    into our sandbox without touching any pathlib internals.

    PA-306: env mutation goes through ``env_bag`` (auto-revert).
    """
    home = tmp_path / "home"
    (home / ".scitex" / "agent-container" / "agents").mkdir(parents=True)
    env_bag.setenv("HOME", str(home))
    # Don't let an outer test session leak its YAML dirs in.
    env_bag.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    env_bag.delenv("SAC_AGENT_SCOPE")
    return home / ".scitex" / "agent-container" / "agents"


def _mkagent(root: Path, name: str) -> None:
    """Create a minimal valid <root>/<name>/spec.yaml fixture."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec: { runtime: apptainer }\n"
        )
    )


def test_enumerate_agent_names_returns_all_created_agents(agent_root: Path):
    # Arrange
    _mkagent(agent_root, "alpha")
    _mkagent(agent_root, "beta")
    _mkagent(agent_root, "gamma")
    # Act
    names = enumerate_agent_names()
    # Assert
    assert {"alpha", "beta", "gamma"}.issubset(set(names))


def test_resolve_with_prefix_returns_spec_for_exact_match(agent_root: Path):
    # Arrange
    _mkagent(agent_root, "alpha")
    # Act
    p = resolve_with_prefix("alpha")
    # Assert
    assert p.endswith("/alpha/spec.yaml")


@pytest.fixture
def unique_prefix_resolution(agent_root, capsys):
    """Resolve ``polish-`` against a single ``polish-clew`` agent."""
    # Arrange
    _mkagent(agent_root, "polish-clew")
    # Act
    path = resolve_with_prefix("polish-")
    err = capsys.readouterr().err
    return path, err


def test_resolve_with_prefix_unique_match_returns_spec_path(
    unique_prefix_resolution,
):
    # Arrange
    path, _err = unique_prefix_resolution
    # Act
    suffix_ok = path.endswith("/polish-clew/spec.yaml")
    # Assert
    assert suffix_ok


def test_resolve_with_prefix_unique_match_emits_matched_name_to_stderr(
    unique_prefix_resolution,
):
    # Arrange
    _path, err = unique_prefix_resolution
    # Act
    contained = "polish-clew" in err
    # Assert
    assert contained


def test_resolve_with_prefix_unique_match_announces_prefix_match_hint(
    unique_prefix_resolution,
):
    # Arrange
    _path, err = unique_prefix_resolution
    # Act
    contained = "prefix match" in err
    # Assert
    assert contained


@pytest.fixture
def ambiguous_prefix_error(agent_root):
    """Trigger ``AmbiguousAgent`` from three ``sai-*`` agents."""
    # Arrange
    _mkagent(agent_root, "sai-factorout")
    _mkagent(agent_root, "sai-test")
    _mkagent(agent_root, "sai-test2")
    # Act
    with pytest.raises(AmbiguousAgent) as exc:
        resolve_with_prefix("sai")
    return exc.value


def test_resolve_with_prefix_ambiguous_error_records_prefix(
    ambiguous_prefix_error,
):
    # Arrange
    err = ambiguous_prefix_error
    # Act
    prefix = err.prefix
    # Assert
    assert prefix == "sai"


def test_resolve_with_prefix_ambiguous_error_lists_all_matches(
    ambiguous_prefix_error,
):
    # Arrange
    err = ambiguous_prefix_error
    # Act
    matches = sorted(err.matches)
    # Assert
    assert matches == ["sai-factorout", "sai-test", "sai-test2"]


def test_resolve_with_prefix_no_match_raises_file_not_found_error(
    agent_root: Path,
):
    # Arrange
    bogus = "ghost-agent-no-prefix-hits"
    # Act
    raises_ctx = pytest.raises(FileNotFoundError)
    # Assert
    with raises_ctx:
        resolve_with_prefix(bogus)


def test_resolve_with_prefix_returns_path_argument_unchanged(
    agent_root: Path, tmp_path: Path
):
    """A path argument must NOT trigger prefix matching — the user
    provided an explicit file path, honour it."""
    # Arrange
    yaml_path = tmp_path / "explicit.yaml"
    yaml_path.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec: { runtime: apptainer }\n"
        )
    )
    # Act
    result = resolve_with_prefix(str(yaml_path))
    # Assert
    assert result == str(yaml_path)


@pytest.fixture
def ambiguous_agent_str() -> str:
    """Render an ``AmbiguousAgent`` exception to its string form."""
    # Arrange
    err = AmbiguousAgent("sai", ["sai-test", "sai-factorout"])
    # Act
    return str(err)


@pytest.mark.parametrize(
    "fragment",
    ["sai", "sai-test", "sai-factorout"],
)
def test_ambiguous_agent_str_contains_prefix_and_each_match(
    ambiguous_agent_str, fragment
):
    # Arrange
    rendered = ambiguous_agent_str
    # Act
    contained = fragment in rendered
    # Assert
    assert contained


def test_user_agents_dir_honours_scitex_dir(env_bag, tmp_path):
    """The spec search root must follow ``$SCITEX_DIR``, not bare ``~``.

    Regression, measured on Spartan 2026-07-14: a remote start dispatched
    with ``SCITEX_DIR=<registry root>`` still searched
    ``~/.scitex/agent-container/agents/`` — on that host a symlink into an
    unrelated paper project — and reported the agent "not found" for a spec
    that had just been rsynced, correctly, into the registry-declared root.
    Resolving the state root and then IGNORING it is worse than never
    resolving it. sac's own docs already call this root "relocatable via
    $SCITEX_DIR"; this pins that promise.
    """
    # Arrange
    from scitex_agent_container.config._resolve import _user_agents_dir

    env_bag.setenv("SCITEX_DIR", str(tmp_path / "relocated"))
    # Act
    resolved = _user_agents_dir()
    # Assert
    assert resolved == tmp_path / "relocated" / "agent-container" / "agents"


def test_user_agents_dir_unset_scitex_dir_is_home_rooted(env_bag):
    """Unset ``$SCITEX_DIR`` → byte-identical to the historical behaviour."""
    # Arrange
    from scitex_agent_container.config._resolve import _user_agents_dir

    env_bag.delenv("SCITEX_DIR")
    # Act
    resolved = _user_agents_dir()
    # Assert
    assert resolved == Path.home() / ".scitex" / "agent-container" / "agents"
