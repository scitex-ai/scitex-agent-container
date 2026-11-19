"""Per-agent caller credentials (``_listen.agent_tokens``).

Real files under ``tmp_path`` — every function takes its registry dir and
state dir explicitly, so no env or module state is touched. No mocks.
AAA-marked, one assert per test.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from scitex_agent_container._listen.agent_tokens import (
    commit_agent_token,
    pending_token_path,
    resolve_agent_token,
    stage_agent_token,
    token_sha256,
)


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "registry", tmp_path / "state"


def test_staged_token_does_not_authenticate_before_commit(tmp_path: Path):
    # Arrange — staging is what `explain` / `--dry-run` reach; it must
    # activate nothing.
    registry, state = _dirs(tmp_path)
    token = stage_agent_token(state)
    # Act
    got = resolve_agent_token(token, registry_dir=registry)
    # Assert
    assert got is None


def test_committed_token_resolves_to_its_agent_name(tmp_path: Path):
    # Arrange
    registry, state = _dirs(tmp_path)
    token = stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    # Act
    got = resolve_agent_token(token, registry_dir=registry)
    # Assert
    assert got == "dev-agent"


def test_staging_twice_returns_the_same_pending_token(tmp_path: Path):
    # Arrange — the Hermes profile and the argv are rendered separately
    # within one launch and must carry one token.
    _, state = _dirs(tmp_path)
    first = stage_agent_token(state)
    # Act
    second = stage_agent_token(state)
    # Assert
    assert second == first


def test_next_start_commit_invalidates_previous_token(tmp_path: Path):
    # Arrange — two consecutive starts of the same agent.
    registry, state = _dirs(tmp_path)
    old = stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    # Act
    got = resolve_agent_token(old, registry_dir=registry)
    # Assert
    assert got is None


def test_commit_without_staged_token_revokes_previous_record(tmp_path: Path):
    # Arrange — a launch that carries no token must not inherit the old one.
    registry, state = _dirs(tmp_path)
    old = stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    # Act
    got = resolve_agent_token(old, registry_dir=registry)
    # Assert
    assert got is None


def test_commit_discards_the_pending_copy(tmp_path: Path):
    # Arrange
    registry, state = _dirs(tmp_path)
    stage_agent_token(state)
    # Act
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    # Assert
    assert not pending_token_path(state).exists()


def test_server_record_stores_the_token_sha256(tmp_path: Path):
    # Arrange
    registry, state = _dirs(tmp_path)
    token = stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    # Act
    record = json.loads((registry / "dev-agent.json").read_text(encoding="utf-8"))
    # Assert
    assert record["sha256"] == token_sha256(token)


def test_server_record_never_contains_the_raw_token(tmp_path: Path):
    # Arrange
    registry, state = _dirs(tmp_path)
    token = stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    # Act
    raw = (registry / "dev-agent.json").read_text(encoding="utf-8")
    # Assert
    assert token not in raw


def test_server_record_file_is_owner_only(tmp_path: Path):
    # Arrange
    registry, state = _dirs(tmp_path)
    stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    # Act
    mode = stat.S_IMODE((registry / "dev-agent.json").stat().st_mode)
    # Assert
    assert mode == 0o600


def test_pending_token_file_is_owner_only(tmp_path: Path):
    # Arrange
    _, state = _dirs(tmp_path)
    stage_agent_token(state)
    # Act
    mode = stat.S_IMODE(pending_token_path(state).stat().st_mode)
    # Assert
    assert mode == 0o600


def test_record_whose_name_disagrees_with_its_file_is_ignored(tmp_path: Path):
    # Arrange — a record not written by commit_agent_token (renamed file).
    registry, state = _dirs(tmp_path)
    token = stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    (registry / "dev-agent.json").rename(registry / "other-agent.json")
    # Act
    got = resolve_agent_token(token, registry_dir=registry)
    # Assert
    assert got is None


def test_unreadable_record_does_not_break_resolution(tmp_path: Path):
    # Arrange — a damaged record next to a good one.
    registry, state = _dirs(tmp_path)
    token = stage_agent_token(state)
    commit_agent_token("dev-agent", state_dir=state, registry_dir=registry)
    (registry / "broken.json").write_text("{not json", encoding="utf-8")
    # Act
    got = resolve_agent_token(token, registry_dir=registry)
    # Assert
    assert got == "dev-agent"


def test_commit_refuses_a_path_traversing_agent_name(tmp_path: Path):
    # Arrange
    registry, state = _dirs(tmp_path)
    stage_agent_token(state)
    # Act
    try:
        commit_agent_token("../escape", state_dir=state, registry_dir=registry)
        refused = None
    except ValueError as exc:
        refused = str(exc)
    # Assert
    assert "not usable" in (refused or "")
