"""``scratch_root:`` / ``scratch_root_reason:`` in config.yaml (ADR-0024).

Pins :func:`_host_config_blocks._parse_scratch` through the real loader.
The block decides where every agent's ``/uvwork`` is bound from, so the
properties under test are the ones that keep a half-written decision from
reading as a whole one:

* a MISSING block stays optional — the resolver then probes the default, and
  compute-01 / compute-03 have no ``config.yaml`` at all;
* ``none`` — the written decision to keep ``/uvwork`` in the apptainer
  overlay upper on the root volume — is refused WITHOUT a stated reason,
  because a decision with no reason is indistinguishable from an omission; and
* an orphan ``scratch_root_reason:`` is refused too, since a reason with no
  root is a decision someone started writing and did not finish.

No mocks (PA-306): every test writes a real YAML file at ``tmp_path`` and
lets the real loader read it through the documented env override.
STX-TQ002 AAA markers; one fact per test (PA-307).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scitex_agent_container._state._host_config_blocks import (
    SCRATCH_ROOT_NONE,
    ScratchBlock,
)
from scitex_agent_container._state.host_config import load


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml under tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# Absent — the shape compute-01 and compute-03 are actually in
# ---------------------------------------------------------------------------


def test_a_config_without_the_key_carries_no_scratch_block(cfg_path: Path) -> None:
    # Arrange — two hosts in this fleet have NO config.yaml key at all.
    cfg_path.write_text("host:\n  name: somewhere\n")
    # Act
    scratch = load().scratch
    # Assert
    assert scratch is None


def test_an_empty_config_carries_no_scratch_block(cfg_path: Path) -> None:
    # Arrange — the positive control for the row above: nothing at all.
    cfg_path.write_text("")
    # Act
    scratch = load().scratch
    # Assert
    assert scratch is None


# ---------------------------------------------------------------------------
# A declared path
# ---------------------------------------------------------------------------


def test_a_declared_absolute_path_parses_into_a_block(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text("scratch_root: /scratch\n")
    # Act
    scratch = load().scratch
    # Assert
    assert scratch == ScratchBlock(root="/scratch", reason="")


def test_a_declared_path_is_not_the_none_decision(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text("scratch_root: /scratch\n")
    # Act
    is_none = load().scratch.is_none
    # Assert
    assert is_none is False


def test_a_declared_path_keeps_an_optional_reason(cfg_path: Path) -> None:
    # Arrange — a reason is welcome on a path, just not required.
    cfg_path.write_text("scratch_root: /scratch\nscratch_root_reason: the 3T LV\n")
    # Act
    reason = load().scratch.reason
    # Assert
    assert reason == "the 3T LV"


def test_a_relative_scratch_root_is_refused(cfg_path: Path) -> None:
    # Arrange — a relative root would resolve against whatever CWD sac
    # happened to start in, which is the class of bug ADR-0024 ends.
    cfg_path.write_text("scratch_root: scratch\n")
    # Act
    raised = pytest.raises(ValueError, match="absolute path")
    # Assert
    with raised:
        load()


def test_the_relative_root_refusal_names_the_config_file(cfg_path: Path) -> None:
    # Arrange — the fix is a one-line edit of a file that must be named.
    cfg_path.write_text("scratch_root: scratch\n")
    # Act
    raised = pytest.raises(ValueError, match=re.escape(str(cfg_path)))
    # Assert
    with raised:
        load()


def test_a_non_string_scratch_root_is_refused(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text("scratch_root: 42\n")
    # Act
    raised = pytest.raises(ValueError, match="scratch_root")
    # Assert
    with raised:
        load()


def test_an_empty_scratch_root_is_refused(cfg_path: Path) -> None:
    # Arrange — a quoted empty string is a declaration that declares nothing.
    cfg_path.write_text("scratch_root: ''\n")
    # Act
    raised = pytest.raises(ValueError, match="absolute path")
    # Assert
    with raised:
        load()


# ---------------------------------------------------------------------------
# `none` — a written decision, never the shape a missing line falls into
# ---------------------------------------------------------------------------


def test_none_with_a_reason_parses_into_a_block(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text("scratch_root: none\nscratch_root_reason: root LV is 8T\n")
    # Act
    scratch = load().scratch
    # Assert
    assert scratch == ScratchBlock(root=SCRATCH_ROOT_NONE, reason="root LV is 8T")


def test_none_with_a_reason_reports_itself_as_the_none_decision(
    cfg_path: Path,
) -> None:
    # Arrange
    cfg_path.write_text("scratch_root: none\nscratch_root_reason: root LV is 8T\n")
    # Act
    is_none = load().scratch.is_none
    # Assert
    assert is_none is True


def test_none_without_a_reason_is_refused(cfg_path: Path) -> None:
    # Arrange — keeping /uvwork on the root volume is the failure mode; it
    # must never be reachable by a bare word.
    cfg_path.write_text("scratch_root: none\n")
    # Act
    raised = pytest.raises(ValueError, match="scratch_root_reason")
    # Assert
    with raised:
        load()


def test_none_with_a_whitespace_reason_is_refused(cfg_path: Path) -> None:
    # Arrange — the positive control for the row above: a reason that says
    # nothing is not a reason.
    cfg_path.write_text("scratch_root: none\nscratch_root_reason: '   '\n")
    # Act
    raised = pytest.raises(ValueError, match="scratch_root_reason")
    # Assert
    with raised:
        load()


def test_a_reason_without_a_root_is_refused(cfg_path: Path) -> None:
    # Arrange — half a decision reads exactly like a whole one later.
    cfg_path.write_text("scratch_root_reason: root LV is 8T\n")
    # Act
    raised = pytest.raises(ValueError, match="scratch_root_reason")
    # Assert
    with raised:
        load()


def test_a_non_string_reason_is_refused(cfg_path: Path) -> None:
    # Arrange
    cfg_path.write_text("scratch_root: none\nscratch_root_reason: 7\n")
    # Act
    raised = pytest.raises(ValueError, match="must be a string")
    # Assert
    with raised:
        load()
