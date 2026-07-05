"""Tests for ``runtimes._apptainer_host_env`` — the cargo-bin PATH append.

Incident: inside agent apptainer containers ``rtk`` fails with
``rtk: not found`` because the operator's host ``~/.cargo/bin`` (where
``rtk`` lives) is NOT on the container PATH — the SIF ships its own
cargo at ``/opt/cargo/bin``. Fix: sac sets apptainer's
``APPTAINERENV_APPEND_PATH`` on the apptainer HOST process so apptainer
APPENDS the host ``~/.cargo/bin`` to the container PATH at launch.

The pure helper :func:`host_cargo_bin_append_env` computes that env
addition. These tests pin its three branches:

  * ``~/.cargo/bin`` EXISTS  → returns the directive with that path.
  * ``~/.cargo/bin`` ABSENT  → returns NO directive (skip-if-missing).
  * directive PRE-EXISTS     → cargo bin appended after a ``:``.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — real ``tmp_path``
plus an explicit ``$HOME`` swap so ``~`` expansion is sandboxed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.runtimes._apptainer_host_env import (
    APPTAINER_APPEND_PATH_ENV,
    LEGACY_ENV_DENYLIST,
    host_cargo_bin_append_env,
    scrub_legacy_env,
)


@pytest.fixture
def fake_home(tmp_path: Path) -> Iterator[Path]:
    """Yield a tmp_path-rooted ``$HOME`` so ``~`` expansion is sandboxed."""
    prev = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if prev is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev


def test_cargo_bin_present_returns_directive_with_path(fake_home: Path) -> None:
    """When ``~/.cargo/bin`` exists the helper emits the append directive
    pointing at that absolute host path."""
    # Arrange
    cargo_bin = fake_home / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    # Act
    result = host_cargo_bin_append_env({})
    # Assert
    assert result == {APPTAINER_APPEND_PATH_ENV: str(cargo_bin)}


def test_cargo_bin_absent_returns_no_directive(fake_home: Path) -> None:
    """Skip-if-missing: no ``~/.cargo/bin`` on the host → no directive."""
    # Arrange: fake_home has no .cargo/bin dir created.
    base_env: dict[str, str] = {}
    # Act
    result = host_cargo_bin_append_env(base_env)
    # Assert
    assert APPTAINER_APPEND_PATH_ENV not in result


def test_preexisting_directive_is_appended_after_colon(fake_home: Path) -> None:
    """A pre-set ``APPTAINERENV_APPEND_PATH`` is preserved and the cargo
    bin appended after a ``:`` separator (never clobbered)."""
    # Arrange
    cargo_bin = fake_home / ".cargo" / "bin"
    cargo_bin.mkdir(parents=True)
    base_env = {APPTAINER_APPEND_PATH_ENV: "/opt/extra/bin"}
    # Act
    result = host_cargo_bin_append_env(base_env)
    # Assert
    assert result[APPTAINER_APPEND_PATH_ENV] == f"/opt/extra/bin:{cargo_bin}"


# ----------------------------------------------------------------------
# scrub_legacy_env — INCIDENT 2026-07-05 (apptainer ambient-env passthrough
# leaking stale pre-rename scitex-todo env vars into containers).
# ----------------------------------------------------------------------
def test_scrub_legacy_env_removes_denylisted_keys() -> None:
    """Every :data:`LEGACY_ENV_DENYLIST` key is stripped from the output,
    even when it is present in the base env passed in (simulating a
    stale export surviving in the launching shell's ambient env)."""
    # Arrange
    base_env = {name: "stale-value" for name in LEGACY_ENV_DENYLIST}
    base_env["UNRELATED_VAR"] = "keep-me"
    # Act
    result = scrub_legacy_env(base_env)
    # Assert
    assert not (set(result) & LEGACY_ENV_DENYLIST)


def test_scrub_legacy_env_keeps_unrelated_keys() -> None:
    """Non-denylisted keys survive the scrub untouched."""
    # Arrange
    base_env = {"SCITEX_TODO_AGENT_ID": "proj-x", "PATH": "/usr/bin"}
    # Act
    result = scrub_legacy_env(base_env)
    # Assert
    assert result == base_env


def test_scrub_legacy_env_does_not_mutate_input() -> None:
    """Pure function — the caller's dict (often a live ``os.environ`` copy)
    must be left untouched; only the returned copy is scrubbed."""
    # Arrange
    base_env = {"SCITEX_TODO_AGENT": "stale", "PATH": "/usr/bin"}
    original = dict(base_env)
    # Act
    scrub_legacy_env(base_env)
    # Assert
    assert base_env == original
