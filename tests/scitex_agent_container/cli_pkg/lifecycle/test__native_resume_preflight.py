"""Real retained native files qualify exact resume without Claude history."""

import json
from types import SimpleNamespace

import pytest

from scitex_agent_container.cli_pkg.lifecycle._native_resume_preflight import (
    preflight_native_resume_id,
)
from scitex_agent_container.cli_pkg.lifecycle._resume_preflight import (
    ResumePreflightError,
)

THREAD = "01a0fdf5-a004-75a3-ae59-86f9a7eac19a"


@pytest.fixture
def native_history(tmp_path):
    root = tmp_path / "private-codex-home"
    directory = root / "sessions" / "2026" / "10" / "02"
    directory.mkdir(parents=True)
    file = directory / f"rollout-2026-10-02T18-52-08-{THREAD}.jsonl"
    row = {"type": "session_meta", "payload": {"id": THREAD, "source": "cli"}}
    file.write_text(json.dumps(row) + "\n")
    return SimpleNamespace(name="research"), root, file


def test_exact_native_header_qualifies_without_claude_store(native_history):
    # Arrange
    config, root, _ = native_history
    # Act
    result = preflight_native_resume_id(config, THREAD, native_home=root)
    # Assert
    assert result == THREAD


def _rewrite_header(file, updates):
    row = json.loads(file.read_text())
    row["payload"].update(updates)
    file.write_text(json.dumps(row) + "\n")


def _escape_history(file, tmp_path):
    foreign = tmp_path / "foreign-history"
    foreign.write_bytes(file.read_bytes())
    file.unlink()
    file.symlink_to(foreign)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda file, tmp: _rewrite_header(
                file,
                {
                    "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": THREAD}}
                    }
                },
            ),
            id="child",
        ),
        pytest.param(
            lambda file, tmp: _rewrite_header(
                file, {"id": "01a0fdd8-24b2-7b23-a264-4ae60f30245b"}
            ),
            id="different-id",
        ),
        pytest.param(
            lambda file, tmp: file.write_text(file.read_text().rstrip("\n")),
            id="partial",
        ),
        pytest.param(lambda file, tmp: file.unlink(), id="missing"),
        pytest.param(
            lambda file, tmp: (
                file.parent / f"rollout-copy-{THREAD}.jsonl"
            ).write_bytes(file.read_bytes()),
            id="duplicate",
        ),
        pytest.param(_escape_history, id="escape"),
    ],
)
def test_invalid_native_identity_refuses_before_lifecycle(
    native_history, tmp_path, mutate
):
    # Arrange
    config, root, file = native_history
    mutate(file, tmp_path)
    # Act
    # Assert
    with pytest.raises(ResumePreflightError):
        preflight_native_resume_id(config, THREAD, native_home=root)


def test_other_home_cannot_supply_missing_native_thread(native_history, tmp_path):
    # Arrange
    config, _, _ = native_history
    # Act
    # Assert
    with pytest.raises(ResumePreflightError, match="absent"):
        preflight_native_resume_id(config, THREAD, native_home=tmp_path / "other-home")


def test_noncanonical_native_uuid_is_refused(native_history):
    # Arrange
    config, root, _ = native_history
    # Act
    # Assert
    with pytest.raises(ResumePreflightError, match="exact thread UUID"):
        preflight_native_resume_id(config, THREAD.upper(), native_home=root)
