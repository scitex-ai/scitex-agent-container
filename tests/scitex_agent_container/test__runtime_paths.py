"""Tests for the runtime-base-dir resolver + its call sites.

Covers the ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` relocation knob:
env set => every runtime path resolves under it; env unset => the
historical ``~/.scitex/agent-container/runtime`` default (unchanged).

Env mutation uses the ``env_save_restore`` fixture (conftest) — the
no-mocks-ecosystem honest replacement for ``monkeypatch.setenv`` — which
sets real ``os.environ`` keys and reverts on teardown.

AAA markers + one-fact-per-test per the package TQ convention.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from scitex_agent_container._runtime_paths import RUNTIME_DIR_ENV, runtime_base_dir

_HISTORICAL_DEFAULT = Path(os.path.expanduser("~/.scitex/agent-container/runtime"))


def test_env_unset_returns_historical_default(env_save_restore):
    # Arrange
    env_save_restore.delete(RUNTIME_DIR_ENV)
    # Act
    got = runtime_base_dir()
    # Assert
    assert got == _HISTORICAL_DEFAULT


def test_env_set_relocates_base(env_save_restore, tmp_path):
    # Arrange
    env_save_restore.set(RUNTIME_DIR_ENV, str(tmp_path / "sac-rt"))
    # Act
    got = runtime_base_dir()
    # Assert
    assert got == tmp_path / "sac-rt"


def test_env_set_is_expanded_and_absolute(env_save_restore):
    # Arrange
    env_save_restore.set(RUNTIME_DIR_ENV, "~/some-node-local-rt")
    # Act
    got = runtime_base_dir()
    # Assert
    assert got == Path(os.path.expanduser("~/some-node-local-rt"))


def test_empty_env_falls_back_to_default(env_save_restore):
    # Arrange
    env_save_restore.set(RUNTIME_DIR_ENV, "")
    # Act
    got = runtime_base_dir()
    # Assert
    assert got == _HISTORICAL_DEFAULT


@pytest.mark.parametrize(
    "module_path, attr, subpath",
    [
        ("scitex_agent_container._state.state_db", "DEFAULT_DB_PATH", "state.db"),
        ("scitex_agent_container._state.registry", "REGISTRY_DIR", "registry"),
        ("scitex_agent_container._runners._session_state", "DEFAULT_STATE_ROOT", ""),
    ],
)
def test_env_relocates_module_constant(
    env_save_restore, tmp_path, module_path, attr, subpath
):
    # Arrange — clear the per-file override envs so the RUNTIME_DIR fallback
    # is what's exercised; reload so the module-level constant recomputes.
    #
    # The final reload is delegated to ``env_save_restore`` rather than done in
    # a ``finally`` here, and that is the whole fix: reloading before the env is
    # restored re-derives the constant from an env var this test has just
    # dropped, which pins it at the operator's real $HOME for every remaining
    # test in this xdist worker. See the fixture's docstring.
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_STATE_DB")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    env_save_restore.set(RUNTIME_DIR_ENV, str(tmp_path / "rt"))
    mod = importlib.reload(importlib.import_module(module_path))
    env_save_restore.reload_after_restore(mod)
    expected = tmp_path / "rt" / subpath if subpath else tmp_path / "rt"
    # Act
    got = getattr(mod, attr)
    # Assert
    assert Path(got) == expected


def test_per_file_state_db_override_still_wins(env_save_restore, tmp_path):
    # Arrange — explicit STATE_DB override must beat RUNTIME_DIR.
    env_save_restore.set(RUNTIME_DIR_ENV, str(tmp_path / "rt"))
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_STATE_DB", str(tmp_path / "explicit" / "s.db")
    )
    mod = importlib.reload(
        importlib.import_module("scitex_agent_container._state.state_db")
    )
    env_save_restore.reload_after_restore(mod)
    # Act
    got = mod.DEFAULT_DB_PATH
    # Assert
    assert Path(got) == tmp_path / "explicit" / "s.db"
