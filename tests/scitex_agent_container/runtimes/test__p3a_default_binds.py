"""Tests for ``runtimes._p3a_default_binds`` — fleet-default bind injection.

P3a-2 (operator directive ``feedback_scitex_todo_single_shared_store``,
lead a2a ``214dd26d3fd24e088c75a34329895fa4``): every sac-launched
agent's apptainer container gets the scitex-todo single shared store
bind even if its spec doesn't carry the explicit line.

The helper:

  * Filters fleet defaults by host-source existence (a missing
    ``~/.scitex/todo/`` produces NO bind, NO crash).
  * Lets an explicit ``spec.apptainer.binds`` entry to the SAME
    destination override the default (de-dup by destination only).
  * Returns bind strings ready for ``apptainer --bind``.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — uses real
``tmp_path`` plus an explicit ``HOME`` swap via ``os.environ``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.runtimes._p3a_default_binds import (
    apply_default_binds,
    default_binds_for_host,
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


# ---------------------------------------------------------------------------
# default_binds_for_host — host-existence gate
# ---------------------------------------------------------------------------


def test_default_binds_for_host_returns_todo_bind_when_host_dir_exists(
    fake_home: Path,
) -> None:
    # Arrange
    (fake_home / ".scitex" / "todo").mkdir(parents=True)
    # Act
    binds = default_binds_for_host()
    # Assert
    assert any("/.scitex/todo:" in b for b in binds)


def test_default_binds_for_host_skips_todo_bind_when_host_dir_missing(
    fake_home: Path,
) -> None:
    # Arrange — fake_home (tmp_path) is freshly created with no .scitex
    # subtree; the candidate path therefore does not exist.
    # Act
    binds = default_binds_for_host()
    # Assert
    assert not any("/.scitex/todo:" in b for b in binds)


def test_default_binds_for_host_returns_tuple_for_caller_immutability(
    fake_home: Path,
) -> None:
    # Arrange — even an empty result must be a tuple (caller treats it
    # as read-only fleet config).
    # Act
    result = default_binds_for_host()
    # Assert
    assert isinstance(result, tuple)


# ---------------------------------------------------------------------------
# apply_default_binds — explicit spec overrides default
# ---------------------------------------------------------------------------


def test_apply_default_binds_prepends_defaults_when_spec_has_no_overlap(
    fake_home: Path,
) -> None:
    # Arrange
    (fake_home / ".scitex" / "todo").mkdir(parents=True)
    spec_binds = ["~/proj:/home/agent/proj:ro"]
    # Act
    result = apply_default_binds(spec_binds)
    # Assert — first entry is the P3a-2 default, second is the spec entry.
    assert "/.scitex/todo:" in result[0] and result[-1] == "~/proj:/home/agent/proj:ro"


def test_apply_default_binds_lets_explicit_spec_entry_override_default(
    fake_home: Path,
) -> None:
    # Arrange — spec carries the SAME destination as the default; the
    # spec wins (de-dup by destination).
    (fake_home / ".scitex" / "todo").mkdir(parents=True)
    spec_binds = ["/tmp/operator-todo:/home/agent/.scitex/todo:rw"]
    # Act
    result = apply_default_binds(spec_binds)
    # Assert — exactly one entry whose destination is /home/agent/.scitex/todo.
    todo_entries = [b for b in result if "/home/agent/.scitex/todo" in b]
    assert todo_entries == ["/tmp/operator-todo:/home/agent/.scitex/todo:rw"]


def test_apply_default_binds_returns_spec_only_when_host_dir_missing(
    fake_home: Path,
) -> None:
    # Arrange
    spec_binds = ["~/proj:/home/agent/proj:ro"]
    # Act
    result = apply_default_binds(spec_binds)
    # Assert
    assert result == spec_binds


def test_apply_default_binds_handles_empty_spec_binds(fake_home: Path) -> None:
    # Arrange
    (fake_home / ".scitex" / "todo").mkdir(parents=True)
    # Act
    result = apply_default_binds([])
    # Assert
    assert any("/.scitex/todo:" in b for b in result)


def test_apply_default_binds_accepts_iterable_of_strings(fake_home: Path) -> None:
    # Arrange — caller passes a generator, not a list.
    (fake_home / ".scitex" / "todo").mkdir(parents=True)
    spec_binds_gen = (s for s in ["~/proj:/home/agent/proj:ro"])
    # Act
    result = apply_default_binds(spec_binds_gen)
    # Assert
    assert "~/proj:/home/agent/proj:ro" in result


def test_apply_default_binds_preserves_explicit_spec_bind_order(
    fake_home: Path,
) -> None:
    # Arrange
    (fake_home / ".scitex" / "todo").mkdir(parents=True)
    spec_binds = [
        "~/proj:/home/agent/proj:ro",
        "~/.gitconfig:/home/agent/.gitconfig:ro",
    ]
    # Act
    result = apply_default_binds(spec_binds)
    # Assert — spec order preserved AFTER the defaults.
    assert result[-2:] == spec_binds


# ---------------------------------------------------------------------------
# 2026-06-13 SAC overlay stopgap — host scitex_agent_container -> in-SIF install
# (lead a2a b6f3916c; removable once a SIF rebuild folds in the new install)
# ---------------------------------------------------------------------------


def test_default_binds_returns_sac_overlay_when_host_repo_exists(
    fake_home: Path,
) -> None:
    # Arrange — synthesise the canonical host repo path under the
    # sandboxed $HOME so the helper's expanduser() check picks it up.
    sac_src = (
        fake_home / "proj" / "scitex-agent-container" / "src" / "scitex_agent_container"
    )
    sac_src.mkdir(parents=True)
    # Act
    binds = default_binds_for_host()
    # Assert — destination path is the in-SIF site-packages location.
    assert any(
        ":/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container:ro" in b
        for b in binds
    )


def test_default_binds_skips_sac_overlay_when_host_repo_missing(
    fake_home: Path,
) -> None:
    # Arrange — fake_home (tmp_path) has no proj/scitex-agent-container
    # subtree; deploy-host case where the operator hasn't cloned the
    # repo at the canonical path.
    # Act
    binds = default_binds_for_host()
    # Assert
    assert not any(
        "/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container" in b
        for b in binds
    )


def test_apply_default_binds_lets_explicit_spec_override_sac_overlay(
    fake_home: Path,
) -> None:
    # Arrange — operator pins a custom host source for the overlay
    # via spec; the spec entry MUST win (de-dup by destination).
    sac_src = (
        fake_home / "proj" / "scitex-agent-container" / "src" / "scitex_agent_container"
    )
    sac_src.mkdir(parents=True)
    custom_override = (
        "/opt/local-sac-src"
        ":/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container:rw"
    )
    spec_binds = [custom_override]
    # Act
    result = apply_default_binds(spec_binds)
    # Assert — exactly one entry whose destination is the in-SIF install path.
    sac_entries = [
        b
        for b in result
        if "/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container" in b
    ]
    assert sac_entries == [custom_override]
