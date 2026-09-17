from __future__ import annotations

import os
import subprocess

import pytest

from scitex_agent_container.cli_pkg import _hermes_source as source


def test_pin_names_the_validated_sac_hermes_source() -> None:
    # Arrange
    expected = (
        "https://github.com/ywatanabe1989/hermes-agent.git",
        "9ca9b7e5b9092465d37e4af0c2132aed188af5dd",
    )

    # Act
    pin = (source.HERMES_REPOSITORY, source.HERMES_COMMIT)

    # Assert — never replace this immutable pair with a mutable PR ref.
    assert pin == expected


def test_explicit_source_must_contain_pinned_commit(tmp_path):
    # Arrange
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    saved = os.environ.get(source.HERMES_SOURCE_ENV)
    os.environ[source.HERMES_SOURCE_ENV] = str(repository)

    # Act
    # Assert
    try:
        with pytest.raises(
            source.HermesSourceError, match="must name a git repository"
        ):
            source.resolve_hermes_repo()
    finally:
        if saved is None:
            os.environ.pop(source.HERMES_SOURCE_ENV, None)
        else:
            os.environ[source.HERMES_SOURCE_ENV] = saved


def test_staged_heartbeat_lifecycle_events_ignore_disabled_display_mode(tmp_path):
    # Arrange — these are the exact two gates in the pinned Hermes source.
    staged = tmp_path / "hermes-agent-src"
    module = staged / "tui_gateway" / "tool_progress.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        """\
def _emit_tool_lifecycle(event, sid, name, args, payload):
    if not _connector_tool_lifecycle(name, args):
        return _emit(event, sid, payload)
    return _emit(event, sid, payload)

def start(sid, name, args, payload):
    if (_tool_progress_enabled(sid) or _tool_lifecycle_required_for_ui(name)
            or _connector_tool_lifecycle(name, args)):
        _emit_tool_lifecycle("tool.start", sid, name, args, payload)

def complete(sid, name, args, payload):
    if (_tool_progress_enabled(sid) or payload.get("inline_diff") or _tool_lifecycle_required_for_ui(name)
            or name in _TODO_TOOL_NAMES or _connector_tool_lifecycle(name, args)):
        _emit_tool_lifecycle("tool.complete", sid, name, args, payload)
""",
        encoding="utf-8",
    )

    # Act
    source._patch_hermes_lifecycle_instrumentation(staged)
    patched = module.read_text(encoding="utf-8")

    # Assert — display.tool_progress=off and /focus may hide UI chrome, but
    # heartbeat instrumentation must still receive every lifecycle event.
    assert (
        "_tool_progress_enabled(sid) or" not in patched,
        patched.count("SAC heartbeat instrumentation is display-independent"),
        "SAC heartbeat instrumentation replay-only" in patched,
    ) == (True, 2, True)


def test_stage_exports_pinned_tree_without_git_metadata(tmp_path):
    # Arrange
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "pyproject.toml").write_text("[project]\nname='hermes'\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    saved_commit = source.HERMES_COMMIT
    saved_repository = source.HERMES_REPOSITORY
    saved_source = os.environ.get(source.HERMES_SOURCE_ENV)
    source.HERMES_COMMIT = commit
    source.HERMES_REPOSITORY = "https://example.invalid/hermes-agent.git"
    os.environ[source.HERMES_SOURCE_ENV] = str(repository)

    build_context = tmp_path / "build-context"
    build_context.mkdir()

    # Act
    try:
        staged = source.stage_hermes_source(build_context)
    finally:
        source.HERMES_COMMIT = saved_commit
        source.HERMES_REPOSITORY = saved_repository
        if saved_source is None:
            os.environ.pop(source.HERMES_SOURCE_ENV, None)
        else:
            os.environ[source.HERMES_SOURCE_ENV] = saved_source

    # Assert
    assert (
        (staged / "pyproject.toml").is_file()
        and (staged / "SAC_UPSTREAM_COMMIT").read_text() == f"{commit}\n"
        and (staged / "SAC_UPSTREAM_REPOSITORY").read_text()
        == "https://example.invalid/hermes-agent.git\n"
        and not (staged / ".git").exists()
    )
