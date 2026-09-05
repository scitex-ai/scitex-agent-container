"""Tests for ``runtimes._apptainer_provider_cfg`` (the seeded provider config dir).

A provider-backed engine points Claude Code at a per-agent
``CLAUDE_CONFIG_DIR``. Measured 2026-09-05: left unseeded, the TUI ran its
first-run wizard and parked on the OAuth sign-in screen with the gateway
URL, model and key all present in its environment. These tests pin the
seed: the onboarding gate, the workspace-trust entry, the pre-approved key,
the one-time move of a legacy dir, and the bind that mounts the result.

Real seams only (no mocks): real directories under ``tmp_path``, real JSON.
One observable fact per test, AAA markers, descriptive names.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container.runtimes._apptainer_provider_cfg import (
    KEY_SUFFIX_LEN,
    approve_api_key,
    container_config_dir,
    host_config_dir,
    legacy_scratch_config_dir,
    provider_config_dir_flags,
    seed_provider_config_dir,
)

KEY = "sk-fleet-gateway-0123456789abcdefghijklmnopqrstuvwxyz"


def _seed(tmp_path: Path, name: str = "biz", key: str = KEY) -> Path:
    return seed_provider_config_dir(
        state_dir=tmp_path / "state",
        name=name,
        workdir=str(tmp_path / "wd"),
        api_key=key,
    )


def _read(host: Path) -> dict:
    return json.loads((host / ".claude.json").read_text())


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def test_the_container_dir_is_namespaced_by_agent_name():
    # Arrange
    name = "bulk7"
    # Act
    path = container_config_dir(name)
    # Assert
    assert path == "/tmp/sac-bulk7-provider-cfg"


def test_the_host_dir_lives_under_the_state_dir(tmp_path: Path):
    # Arrange
    state = tmp_path / "state"
    # Act
    path = host_config_dir(state)
    # Assert
    assert path == state / "provider-cfg"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seed_creates_the_host_dir(tmp_path: Path):
    # Arrange
    state = tmp_path / "state"
    # Act
    _seed(tmp_path)
    # Assert
    assert (state / "provider-cfg").is_dir()


def test_seed_marks_global_onboarding_complete(tmp_path: Path):
    # Arrange
    state = tmp_path / "state"
    # Act
    _seed(tmp_path)
    # Assert
    assert _read(host_config_dir(state))["hasCompletedOnboarding"] is True


def test_seed_marks_the_workspace_trusted(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # Act
    host = _seed(tmp_path)
    # Assert
    entry = _read(host)["projects"][str(workdir.resolve())]
    assert entry["hasTrustDialogAccepted"] is True


def test_seed_approves_the_key_by_its_trailing_characters(tmp_path: Path):
    # Arrange
    state = tmp_path / "state"
    # Act
    _seed(tmp_path)
    # Assert
    approved = _read(host_config_dir(state))["customApiKeyResponses"]["approved"]
    assert approved == [KEY[-KEY_SUFFIX_LEN:]]


def test_seed_drops_a_stale_rejection_of_the_same_key(tmp_path: Path):
    # Arrange — handyman-01's measured shape: the env key sat in ``rejected``.
    host = host_config_dir(tmp_path / "state")
    host.mkdir(parents=True)
    (host / ".claude.json").write_text(
        json.dumps(
            {
                "customApiKeyResponses": {
                    "approved": [],
                    "rejected": [KEY[-20:], "other-key-suffix"],
                }
            }
        )
    )
    # Act
    _seed(tmp_path)
    # Assert
    assert _read(host)["customApiKeyResponses"]["rejected"] == ["other-key-suffix"]


def test_seed_lists_the_approved_suffix_once_across_starts(tmp_path: Path):
    # Arrange
    _seed(tmp_path)
    # Act
    host = _seed(tmp_path)
    # Assert
    assert _read(host)["customApiKeyResponses"]["approved"].count(KEY[-20:]) == 1


def test_seed_keeps_what_the_tui_already_wrote(tmp_path: Path):
    # Arrange — the TUI's own first-run stub (measured: machineID etc.).
    host = host_config_dir(tmp_path / "state")
    host.mkdir(parents=True)
    (host / ".claude.json").write_text(
        json.dumps({"machineID": "m-1", "numStartups": 7})
    )
    # Act
    _seed(tmp_path)
    # Assert
    data = _read(host)
    assert (data["machineID"], data["numStartups"]) == ("m-1", 7)


def test_seed_writes_the_config_file_owner_only(tmp_path: Path):
    # Arrange
    state = tmp_path / "state"
    # Act
    _seed(tmp_path)
    # Assert
    mode = (host_config_dir(state) / ".claude.json").stat().st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------------------------
# Legacy location (the relocated container /tmp)
# ---------------------------------------------------------------------------


def test_a_legacy_scratch_dir_is_moved_into_the_state_dir(tmp_path: Path):
    # Arrange — an agent that ran before the bind kept its history there.
    legacy = legacy_scratch_config_dir(tmp_path / "state", "biz")
    legacy.mkdir(parents=True)
    (legacy / "history.jsonl").write_text('{"display":"hello"}\n')
    # Act
    host = _seed(tmp_path)
    # Assert
    assert (host / "history.jsonl").read_text() == '{"display":"hello"}\n'


def test_the_legacy_dir_is_gone_after_the_move(tmp_path: Path):
    # Arrange
    legacy = legacy_scratch_config_dir(tmp_path / "state", "biz")
    legacy.mkdir(parents=True)
    (legacy / "history.jsonl").write_text("x\n")
    # Act
    _seed(tmp_path)
    # Assert
    assert not legacy.exists()


def test_an_existing_host_dir_is_never_replaced_by_the_legacy_one(tmp_path: Path):
    # Arrange — both exist; the host dir is the live one and must win.
    host = host_config_dir(tmp_path / "state")
    host.mkdir(parents=True)
    (host / "history.jsonl").write_text("live\n")
    legacy = legacy_scratch_config_dir(tmp_path / "state", "biz")
    legacy.mkdir(parents=True)
    (legacy / "history.jsonl").write_text("stale\n")
    # Act
    _seed(tmp_path)
    # Assert
    assert (host / "history.jsonl").read_text() == "live\n"


# ---------------------------------------------------------------------------
# approve_api_key on its own
# ---------------------------------------------------------------------------


def test_approve_creates_the_file_when_absent(tmp_path: Path):
    # Arrange
    target = tmp_path / ".claude.json"
    # Act
    approve_api_key(target, KEY)
    # Assert
    assert json.loads(target.read_text())["customApiKeyResponses"]["approved"] == [
        KEY[-20:]
    ]


def test_approve_reports_no_change_when_already_approved(tmp_path: Path):
    # Arrange
    target = tmp_path / ".claude.json"
    approve_api_key(target, KEY)
    # Act
    changed = approve_api_key(target, KEY)
    # Assert
    assert changed is False


def test_approve_leaves_an_unparseable_file_alone(tmp_path: Path):
    # Arrange
    target = tmp_path / ".claude.json"
    target.write_text("{not json")
    # Act
    approve_api_key(target, KEY)
    # Assert
    assert target.read_text() == "{not json"


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def test_flags_bind_the_host_dir_at_the_container_config_dir(tmp_path: Path):
    # Arrange
    state = tmp_path / "state"
    # Act
    flags = provider_config_dir_flags(
        state_dir=state, name="biz", workdir=str(tmp_path / "wd"), api_key=KEY
    )
    # Assert
    assert flags == ["--bind", f"{state / 'provider-cfg'}:/tmp/sac-biz-provider-cfg:rw"]
