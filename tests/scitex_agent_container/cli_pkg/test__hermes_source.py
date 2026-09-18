from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from scitex_agent_container.cli_pkg import _hermes_source as source

PINNED_TOOL_PROGRESS_ANCHORS = """\
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
"""


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
    module.write_text(PINNED_TOOL_PROGRESS_ANCHORS, encoding="utf-8")

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


def test_heartbeat_instrumentation_fails_closed_when_tool_progress_is_absent(tmp_path):
    # Arrange
    staged = tmp_path / "hermes-agent-src"
    staged.mkdir()

    # Act
    # Assert
    with pytest.raises(source.HermesSourceError, match="tool_progress.py is absent"):
        source._patch_hermes_lifecycle_instrumentation(staged)


def test_heartbeat_instrumentation_fails_closed_when_an_anchor_drifts(tmp_path):
    # Arrange
    staged = tmp_path / "hermes-agent-src"
    module = staged / "tui_gateway" / "tool_progress.py"
    module.parent.mkdir(parents=True)
    module.write_text("def changed_upstream_anchor():\n    pass\n", encoding="utf-8")

    # Act
    # Assert
    with pytest.raises(source.HermesSourceError, match="gate drifted"):
        source._patch_hermes_lifecycle_instrumentation(staged)


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
    tool_progress = repository / "tui_gateway" / "tool_progress.py"
    tool_progress.parent.mkdir()
    tool_progress.write_text(PINNED_TOOL_PROGRESS_ANCHORS, encoding="utf-8")
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


def test_exact_pinned_stage_records_hidden_tool_start_without_ui_emission(tmp_path):
    # Arrange — stage tracked bytes from the immutable production pin, then
    # exercise its real replay ring in a clean interpreter.
    repository = source.resolve_hermes_repo()
    saved_source = os.environ.get(source.HERMES_SOURCE_ENV)
    os.environ[source.HERMES_SOURCE_ENV] = str(repository)
    build_context = tmp_path / "exact-pinned-build-context"
    build_context.mkdir()
    try:
        staged = source.stage_hermes_source(build_context)
    finally:
        if saved_source is None:
            os.environ.pop(source.HERMES_SOURCE_ENV, None)
        else:
            os.environ[source.HERMES_SOURCE_ENV] = saved_source
    script = r"""
import json
from tui_gateway import event_replay, tool_progress

event_replay.reset_replay_state()
tool_progress._sessions = {}
tool_progress._connector_lifecycle_is_stale = lambda *_args: False
tool_progress._connector_tool_lifecycle = lambda *_args: False
tool_progress._tool_progress_enabled = lambda _sid: False
tool_progress._tool_lifecycle_required_for_ui = lambda _name: False
tool_progress._tool_ctx = lambda _name, _args: ""
tool_progress._session_verbose = lambda _sid: False
tool_progress._event_frame = lambda event, sid, payload: {
    "jsonrpc": "2.0",
    "method": "event",
    "params": {"type": event, "session_id": sid, "payload": payload},
}
def forbidden_ui_emit(*_args, **_kwargs):
    raise RuntimeError("display-disabled tool lifecycle reached UI emission")
tool_progress._emit = forbidden_ui_emit

tool_progress._on_tool_start("session-1", "call-1", "terminal", {"command": "true"})
print(json.dumps(event_replay.events_since("session-1", 0), sort_keys=True))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(staged), env.get("PYTHONPATH", "")) if part
    )

    # Act
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=staged,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    # Assert
    events = json.loads(completed.stdout) if completed.stdout.strip() else []
    event = events[0] if events else {}
    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    assert (
        completed.returncode,
        completed.stderr,
        (staged / "SAC_UPSTREAM_COMMIT").read_text(encoding="utf-8").strip(),
        event.get("type"),
        payload.get("tool_id"),
        event.get("seq"),
    ) == (0, "", source.HERMES_COMMIT, "tool.start", "call-1", 1)
