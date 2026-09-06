"""The sweep's scope must not depend on the working directory.

Real roots under ``tmp_path``, resolved by the real resolver through the real
env seams. No mocks: the fact under test is which directories a 100-file
rewrite would WRITE INTO, and a mocked resolver would report only what the
test author believed.

**MEASURED, 2026-09-06, with ``$SCITEX_AGENT_CONTAINER_AGENTS_DIR`` unset.**
``default_spec_roots`` resolved through ``user_scope_roots``, whose first
entry is "the first ``.scitex/agent-container/agents`` found by walking upward
from cwd"::

    cwd = the sac repo (this agent's own workdir)
        3 roots, 119 specs   — includes the repo's own sdk-test and self
    cwd = /uvwork/tmp   or   /home/agent
        2 roots, 117 specs

Two defects in one number. The sweep's scope — and with ``--apply``, its write
set — changed with the working directory. And ``git ls-files`` confirms
``.scitex/agent-container/agents/{sdk-test,self}/spec.yaml`` are TRACKED repo
fixtures: from the normal invocation, ``--apply`` would rewrite them. Both
escaped only by accident of unrelated guards, and
``--host-supports-engines local`` — a plausible developer flag — put the
tracked fixture straight back into the write set.

**AND THE ORDER DISAGREED WITH THE LOADER.** ``user_scope_roots`` returns
``[primary, *project_local, *operator_env_dirs]`` while ``config/_resolve``
resolves ``project_local -> primary -> operator_env_dirs``. On a name
collision the sweep would migrate the copy the loader does not load, and
nothing in the report could say which. Dropping the cwd-derived root removes
the disagreement rather than papering over it: what remains — primary, then
the operator dirs — is the loader's own order.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._agents_migrate_engines import (
    default_spec_roots,
    excluded_spec_roots,
)


@contextmanager
def _env(**pairs):
    """Set real env vars for the block, restore exactly what was there.

    The production resolver reads the real environment, so the test sets the
    real environment. ``None`` removes a variable.
    """
    saved = {key: os.environ.get(key) for key in pairs}
    for key, value in pairs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _cwd(where: Path):
    was = os.getcwd()
    os.chdir(where)
    try:
        yield
    finally:
        os.chdir(was)


@pytest.fixture
def repo_with_a_project_registry(tmp_path: Path):
    """A git repo carrying its own ``.scitex/agent-container/agents``.

    The shape the sac checkout itself has: a project-local registry holding
    checked-in test fixtures, discovered by walking upward from cwd. ``.git``
    only has to EXIST for both project-scope resolvers, which is what makes
    this a faithful fixture rather than an approximation.
    """
    repo = tmp_path / "repo"
    project_agents = repo / ".scitex" / "agent-container" / "agents"
    project_agents.mkdir(parents=True)
    (repo / ".git").mkdir()
    primary = tmp_path / "home-scitex" / "agent-container" / "agents"
    primary.mkdir(parents=True)
    operator = tmp_path / "operator" / "agents"
    operator.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with _env(
        SCITEX_AGENT_CONTAINER_AGENTS_DIR=None,
        SCITEX_DIR=tmp_path / "home-scitex",
        SCITEX_AGENT_CONTAINER_YAML_DIRS=operator,
        SAC_AGENT_SCOPE=None,
        SAC_SPEC_CACHE_DISABLE="1",
    ):
        yield repo, project_agents, primary, operator, elsewhere


def test_the_project_local_registry_is_not_swept(
    repo_with_a_project_registry,
) -> None:
    # Arrange — the repo's checked-in fixtures are not the fleet, and
    # `--apply` from the repo would have rewritten tracked files.
    repo, project_agents, _primary, _operator, _elsewhere = repo_with_a_project_registry
    # Act
    with _cwd(repo):
        roots = default_spec_roots()
    # Assert
    assert project_agents not in roots


def test_the_fleet_roots_are_still_swept_from_inside_the_repo(
    repo_with_a_project_registry,
) -> None:
    # Arrange — the control: excluding one root must not cost the others.
    repo, _project, primary, operator, _elsewhere = repo_with_a_project_registry
    # Act
    with _cwd(repo):
        roots = default_spec_roots()
    # Assert
    assert roots == (primary, operator)


def test_the_scope_does_not_change_with_the_working_directory(
    repo_with_a_project_registry,
) -> None:
    # Arrange — measured: 119 specs from the repo, 117 from anywhere else.
    repo, _project, _primary, _operator, elsewhere = repo_with_a_project_registry
    # Act
    with _cwd(repo):
        from_repo = default_spec_roots()
    with _cwd(elsewhere):
        from_elsewhere = default_spec_roots()
    # Assert
    assert from_repo == from_elsewhere


def test_the_order_matches_the_loaders_own_precedence(
    repo_with_a_project_registry,
) -> None:
    # Arrange — resolve_config resolves primary before the operator dirs, and
    # the sweep's earlier-root-wins must agree or it writes the copy the
    # loader does not load.
    repo, _project, primary, operator, _elsewhere = repo_with_a_project_registry
    # Act
    with _cwd(repo):
        roots = default_spec_roots()
    # Assert
    assert list(roots).index(primary) < list(roots).index(operator)


def test_the_excluded_root_is_named_rather_than_dropped(
    repo_with_a_project_registry,
) -> None:
    # Arrange — a root that was resolved and then left out has to be visible,
    # exactly as `roots_absent` is: otherwise the count reads as covering it.
    repo, project_agents, _primary, _operator, _elsewhere = repo_with_a_project_registry
    # Act
    with _cwd(repo):
        excluded = excluded_spec_roots()
    # Assert
    assert excluded == (project_agents,)


def test_nothing_is_reported_excluded_outside_a_project(
    repo_with_a_project_registry,
) -> None:
    # Arrange — the control: no project-local registry, nothing to report.
    _repo, _project, _primary, _operator, elsewhere = repo_with_a_project_registry
    # Act
    with _cwd(elsewhere):
        excluded = excluded_spec_roots()
    # Assert
    assert excluded == ()


def test_an_explicit_project_scope_is_still_honoured(
    repo_with_a_project_registry,
) -> None:
    # Arrange — `SAC_AGENT_SCOPE=project` is an operator asking for project
    # scope BY NAME. Inheriting a cwd is the accident; an explicit request is
    # not, and refusing it would make the flag a lie.
    repo, project_agents, _primary, _operator, _elsewhere = repo_with_a_project_registry
    # Act
    with _cwd(repo), _env(SAC_AGENT_SCOPE="project"):
        roots = default_spec_roots()
    # Assert
    assert project_agents in roots


def test_the_agents_dir_env_var_still_wins_over_everything(
    repo_with_a_project_registry, tmp_path: Path
) -> None:
    # Arrange — the documented override keeps its precedence, and excluding a
    # root must not disturb it.
    repo, _project, _primary, _operator, _elsewhere = repo_with_a_project_registry
    named = tmp_path / "named" / "agents"
    named.mkdir(parents=True)
    # Act
    with _cwd(repo), _env(SCITEX_AGENT_CONTAINER_AGENTS_DIR=named):
        roots = default_spec_roots()
    # Assert
    assert roots == (named,)


def test_an_explicit_agents_dir_reports_no_excluded_root(
    repo_with_a_project_registry, tmp_path: Path
) -> None:
    # Arrange — the env var replaces the whole resolution, so nothing was
    # resolved-and-left-out to report.
    repo, _project, _primary, _operator, _elsewhere = repo_with_a_project_registry
    named = tmp_path / "named" / "agents"
    named.mkdir(parents=True)
    # Act
    with _cwd(repo), _env(SCITEX_AGENT_CONTAINER_AGENTS_DIR=named):
        excluded = excluded_spec_roots()
    # Assert
    assert excluded == ()
