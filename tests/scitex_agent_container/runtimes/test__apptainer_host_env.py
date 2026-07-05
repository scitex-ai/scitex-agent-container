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
    host_cargo_bin_append_env,
    minimal_launch_env,
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
# minimal_launch_env — generic clean-environment allowlist
# (operator directive 2026-07-05). Proves NO ambient var of any name
# survives except the generic system + apptainer-owned namespaces.
# ----------------------------------------------------------------------
def test_minimal_launch_env_drops_arbitrary_poison_var() -> None:
    """A poison var of an arbitrary GENERIC name is dropped — the point
    of the name-agnostic allowlist is that it needs no knowledge of the
    offending var's name."""
    # Arrange
    base = {"PATH": "/usr/bin", "LEAK_TEST_CANARY": "SHOULD_NOT_APPEAR"}
    # Act
    result = minimal_launch_env(base)
    # Assert
    assert "LEAK_TEST_CANARY" not in result


def test_minimal_launch_env_drops_downstream_package_var() -> None:
    """A stale downstream-package var (the reported leak) is dropped
    without sac's code ever naming it."""
    # Arrange
    base = {"PATH": "/usr/bin", "SCITEX_TODO_AGENT": "POISON_LEGACY"}
    # Act
    result = minimal_launch_env(base)
    # Assert
    assert "SCITEX_TODO_AGENT" not in result


def test_minimal_launch_env_keeps_path() -> None:
    """PATH survives so the apptainer binary + its helpers resolve."""
    # Arrange
    base = {"PATH": "/usr/bin:/bin"}
    # Act
    result = minimal_launch_env(base)
    # Assert
    assert result["PATH"] == "/usr/bin:/bin"


def test_minimal_launch_env_keeps_apptainerenv_append_path() -> None:
    """sac's own ``APPTAINERENV_APPEND_PATH`` directive survives (matches
    the ``APPTAINERENV_`` prefix rule) so the cargo-bin PATH append is
    preserved even under the curated launch env."""
    # Arrange
    base = {"PATH": "/usr/bin", APPTAINER_APPEND_PATH_ENV: "/host/cargo/bin"}
    # Act
    result = minimal_launch_env(base)
    # Assert
    assert result[APPTAINER_APPEND_PATH_ENV] == "/host/cargo/bin"


def test_minimal_launch_env_keeps_locale_prefix() -> None:
    """A generic ``LC_*`` locale var survives (prefix rule)."""
    # Arrange
    base = {"PATH": "/usr/bin", "LC_ALL": "C.UTF-8"}
    # Act
    result = minimal_launch_env(base)
    # Assert
    assert result["LC_ALL"] == "C.UTF-8"


def test_minimal_launch_env_does_not_mutate_input() -> None:
    """Pure — the caller's ``base_env`` is untouched."""
    # Arrange
    base = {"PATH": "/usr/bin", "LEAK_TEST_CANARY": "x"}
    # Act
    minimal_launch_env(base)
    # Assert
    assert base == {"PATH": "/usr/bin", "LEAK_TEST_CANARY": "x"}
