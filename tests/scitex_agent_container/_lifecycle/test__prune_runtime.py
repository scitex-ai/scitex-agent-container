"""Tests for the ephemeral-agent runtime/overlay prune (inode hygiene).

Real collaborators only: production ``AgentConfig`` / ``RestartSpec`` /
``ApptainerSpec`` dataclasses, real ``tmp_path`` directories, and the
``env_save_restore`` fixture (conftest) for the runtime-base relocation.

AAA markers + one-fact-per-test per the package TQ convention.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle import _prune_runtime as pr
from scitex_agent_container.config._types import (
    AgentConfig,
    ApptainerSpec,
    RestartSpec,
)

_RT_ENV = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"


def _cfg(name="cap", *, policy="never", prune_on_stop=False, overlay="") -> AgentConfig:
    return AgentConfig(
        name=name,
        restart=RestartSpec(policy=policy, prune_on_stop=prune_on_stop),
        apptainer=ApptainerSpec(overlay=overlay),
    )


# ---------------------------------------------------------------------------
# should_prune_runtime — the conservative gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy, prune_on_stop, expected",
    [
        ("never", True, True),  # opted-in ephemeral → prune
        ("never", False, False),  # default-never coordinator → keep
        ("always", True, False),  # persistent → keep
        ("on-failure", True, False),  # persistent → keep
    ],
)
def test_should_prune_runtime_gate(policy, prune_on_stop, expected):
    # Arrange
    cfg = _cfg(policy=policy, prune_on_stop=prune_on_stop)
    # Act
    got = pr.should_prune_runtime(cfg)
    # Assert
    assert got is expected


# ---------------------------------------------------------------------------
# prune_agent_runtime — removes runtime dir + overlay
# ---------------------------------------------------------------------------


def test_prune_removes_runtime_dir(env_save_restore, tmp_path):
    # Arrange — relocate the runtime base to tmp and seed the state dir.
    env_save_restore.set(_RT_ENV, str(tmp_path / "rt"))
    ss = importlib.reload(
        importlib.import_module("scitex_agent_container._runners._session_state")
    )
    try:
        state_dir = ss.state_dir_for("cap")
        state_dir.mkdir(parents=True)
        (state_dir / "heartbeat.json").write_text("{}")
        # Act
        pr.prune_agent_runtime(_cfg(name="cap", policy="never", prune_on_stop=True))
        # Assert
        assert not state_dir.exists()
    finally:
        env_save_restore.delete(_RT_ENV)
        importlib.reload(ss)


def test_prune_removes_overlay_dir(tmp_path):
    # Arrange — an absolute directory-form overlay with an upper/ layer.
    overlay = tmp_path / "ov"
    (overlay / "upper").mkdir(parents=True)
    cfg = _cfg(name="cap2", policy="never", prune_on_stop=True, overlay=str(overlay))
    # Act
    removed = pr.prune_agent_runtime(cfg)
    # Assert
    assert not overlay.exists() and str(overlay) in removed


def test_prune_missing_dirs_no_crash_returns_empty(tmp_path):
    # Arrange — neither the runtime dir nor the overlay exists.
    cfg = _cfg(
        name="ghost-agent-xyz",
        policy="never",
        prune_on_stop=True,
        overlay=str(tmp_path / "nope"),
    )
    # Act
    removed = pr.prune_agent_runtime(cfg)
    # Assert
    assert removed == []


# ---------------------------------------------------------------------------
# maybe_prune_agent_runtime — gate + prune in one call
# ---------------------------------------------------------------------------


def test_maybe_prune_skips_persistent_agent(tmp_path):
    # Arrange — persistent agent with an existing overlay; must be kept.
    overlay = tmp_path / "ov"
    overlay.mkdir()
    cfg = _cfg(name="coord", policy="always", prune_on_stop=True, overlay=str(overlay))
    # Act
    removed = pr.maybe_prune_agent_runtime(cfg)
    # Assert
    assert overlay.exists() and removed == []


def test_maybe_prune_skips_never_without_optin(tmp_path):
    # Arrange — default-never coordinator (no opt-in); overlay must survive.
    overlay = tmp_path / "ov"
    overlay.mkdir()
    cfg = _cfg(name="coord2", policy="never", prune_on_stop=False, overlay=str(overlay))
    # Act
    removed = pr.maybe_prune_agent_runtime(cfg)
    # Assert
    assert overlay.exists() and removed == []


def test_maybe_prune_removes_opted_in_ephemeral_overlay(tmp_path):
    # Arrange — opted-in ephemeral agent; overlay must be pruned.
    overlay = tmp_path / "ov"
    overlay.mkdir()
    cfg = _cfg(name="cap3", policy="never", prune_on_stop=True, overlay=str(overlay))
    # Act
    removed = pr.maybe_prune_agent_runtime(cfg)
    # Assert
    assert not overlay.exists() and str(overlay) in removed
