"""Tests for the sibling-copy staleness warning (_drift/_sibling.py).

The 2026-08-15 incident: the operator edited the dotfiles SOURCE copy of a
spec while sac launched the runtime copy, and nothing anywhere said the
edit was inert — four hours lost. These tests cover the WARNING that
exists so that four hours never happens again: a DIFFERENT copy of the
spec exists, it is NEWER than the one loaded, and the loaded one is what
actually runs.

PA-306: no mocks. Real files, real symlinks, real mtimes (``os.utime``
with fixed epochs), real git-less "live" trees for the incident case.
SCITEX_DIR is redirected to tmp_path so the canonical user-scope probe
never sees the operator's real ~/.scitex.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003).
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._drift import DriftState, warn_if_spec_source_drifted
from scitex_agent_container._drift._sibling import (
    SIBLING_ROOTS_ENV,
    candidate_sibling_paths,
    find_newer_siblings,
    sibling_warning_lines,
    spec_rel_tail,
    warn_if_newer_sibling,
)

# Fixed epochs so the mtime columns of the warning are assertable: the
# "loaded" spec is one hour OLDER than the "sibling" copy.
_OLD = 1_700_000_000.0
_NEW = 1_700_003_600.0

_TAIL = Path("agent-container") / "agents" / "foo" / "spec.yaml"


def _make_spec(base: Path, agent: str = "foo") -> Path:
    """A spec.yaml at <base>/.scitex/agent-container/agents/<agent>/."""
    spec = base / ".scitex" / "agent-container" / "agents" / agent / "spec.yaml"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("apiVersion: scitex-agent-container/v3\nkind: Agent\n")
    return spec


def _touch(path: Path, epoch: float) -> None:
    """Pin a file's mtime to an exact epoch (both access and modify)."""
    os.utime(path, (epoch, epoch))


def _fmt(epoch: float) -> str:
    """The warning's mtime format, for asserting known epochs verbatim."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


@pytest.fixture
def isolated_scitex_dir(tmp_path: Path, env_save_restore):
    """Redirect SCITEX_DIR to tmp_path so user_root() sees an empty tree."""
    home = tmp_path / "scitex-home"
    home.mkdir()
    env_save_restore.set("SCITEX_DIR", str(home))
    return home


# ---------------------------------------------------------------------------
# spec_rel_tail — the intra-.scitex tail, derived from the path itself
# ---------------------------------------------------------------------------


def test_spec_rel_tail_uses_innermost_scitex_anchor():
    # Arrange
    spec = "/home/op/.scitex/agent-container/agents/foo/spec.yaml"
    # Act
    tail = spec_rel_tail(spec, agent="ignored")
    # Assert
    assert tail == _TAIL


def test_spec_rel_tail_falls_back_to_canonical_when_unanchored():
    # Arrange
    spec = "/home/op/elsewhere/foo/spec.yaml"
    # Act
    tail = spec_rel_tail(spec, agent="foo")
    # Assert
    assert tail == _TAIL


def test_spec_rel_tail_unanchored_without_agent_is_none():
    # Arrange
    spec = "/home/op/elsewhere/spec.yaml"
    # Act
    tail = spec_rel_tail(spec)
    # Assert
    assert tail is None


def test_spec_rel_tail_of_tree_root_is_none():
    # Arrange
    spec = "/home/op/.scitex"
    # Act
    tail = spec_rel_tail(spec)
    # Assert
    assert tail is None


# ---------------------------------------------------------------------------
# candidate_sibling_paths — derivation, no hardcoded host layout
# ---------------------------------------------------------------------------


def test_candidate_paths_probe_configured_tree_root(
    tmp_path: Path, isolated_scitex_dir
):
    # Arrange — the configured root names the .scitex tree ITSELF.
    loaded = _make_spec(tmp_path / "A")
    tree_b = tmp_path / "B" / ".scitex"
    # Act
    candidates = candidate_sibling_paths(loaded, agent="foo", extra_roots=[tree_b])
    # Assert — the sibling sits at <tree>/<tail>
    assert tree_b / _TAIL in candidates


def test_candidate_paths_probe_configured_parent_root(
    tmp_path: Path, isolated_scitex_dir
):
    # Arrange — the configured root names the PARENT of the .scitex tree
    # (the source-tree root, e.g. ~/.dotfiles/src): both depths probed.
    loaded = _make_spec(tmp_path / "A")
    parent_b = tmp_path / "B"
    # Act
    candidates = candidate_sibling_paths(loaded, agent="foo", extra_roots=[parent_b])
    # Assert — the .scitex-depth probe is among the candidates
    assert parent_b / ".scitex" / _TAIL in candidates


def test_candidate_paths_exclude_the_loaded_spec_itself(
    tmp_path: Path, isolated_scitex_dir
):
    # Arrange — the loaded spec's OWN tree is configured as a root.
    loaded = _make_spec(tmp_path / "A")
    # Act
    candidates = candidate_sibling_paths(
        loaded, agent="foo", extra_roots=[tmp_path / "A"]
    )
    # Assert — a file is not a sibling of itself
    assert loaded.resolve() not in {c.resolve() for c in candidates}


# ---------------------------------------------------------------------------
# find_newer_siblings — strictly-newer, stat-cheap, never raises
# ---------------------------------------------------------------------------


def test_find_newer_flags_strictly_newer_sibling(
    tmp_path: Path, isolated_scitex_dir
):
    # Arrange — loaded (tree A) is older; the sibling (tree B, parent form)
    # is one hour newer.
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _OLD)
    _touch(sibling, _NEW)
    # Act
    found = find_newer_siblings(loaded, agent="foo", extra_roots=[tmp_path / "B"])
    # Assert
    assert found == [(sibling, _NEW)]


def test_find_newer_silent_when_sibling_older(tmp_path: Path, isolated_scitex_dir):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _NEW)
    _touch(sibling, _OLD)
    # Act
    found = find_newer_siblings(loaded, agent="foo", extra_roots=[tmp_path / "B"])
    # Assert
    assert found == []


def test_find_newer_silent_when_mtimes_equal(tmp_path: Path, isolated_scitex_dir):
    # Arrange — strictly-newer is the rule; equal is the normal state.
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _NEW)
    _touch(sibling, _NEW)
    # Act
    found = find_newer_siblings(loaded, agent="foo", extra_roots=[tmp_path / "B"])
    # Assert
    assert found == []


def test_find_newer_missing_spec_returns_empty(tmp_path: Path, isolated_scitex_dir):
    # Arrange — nothing at the loaded path; a sibling root is configured.
    loaded = tmp_path / "A" / ".scitex" / _TAIL
    # Act
    found = find_newer_siblings(loaded, agent="foo", extra_roots=[tmp_path / "B"])
    # Assert
    assert found == []


def test_loaded_tree_as_root_does_not_flag_self(
    tmp_path: Path, isolated_scitex_dir
):
    # Arrange — only the loaded spec exists, and its own tree is configured
    # as a root: the self-copy must be excluded, never flagged.
    loaded = _make_spec(tmp_path / "A")
    _touch(loaded, _NEW)
    # Act
    found = find_newer_siblings(loaded, agent="foo", extra_roots=[tmp_path / "A"])
    # Assert
    assert found == []


def test_symlink_sibling_carries_target_mtime(
    tmp_path: Path, isolated_scitex_dir
):
    # Arrange — the sibling PATH is a symlink to a real, newer file: any
    # reader through that path sees the target's mtime, so that is the
    # mtime the warning must report.
    loaded = _make_spec(tmp_path / "A")
    _touch(loaded, _OLD)
    target = tmp_path / "real" / "spec-target.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("apiVersion: scitex-agent-container/v3\n")
    _touch(target, _NEW)
    sibling = tmp_path / "B" / ".scitex" / _TAIL
    sibling.parent.mkdir(parents=True)
    sibling.symlink_to(target)
    # Act
    found = find_newer_siblings(loaded, agent="foo", extra_roots=[tmp_path / "B"])
    # Assert
    assert found == [(sibling, _NEW)]


def test_duplicate_probes_of_one_file_are_deduplicated(
    tmp_path: Path, isolated_scitex_dir
):
    # Arrange — the same sibling file is probed at both configured depths
    # (parent B and tree B/.scitex).
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _OLD)
    _touch(sibling, _NEW)
    # Act
    found = find_newer_siblings(
        loaded, agent="foo", extra_roots=[tmp_path / "B", tmp_path / "B" / ".scitex"]
    )
    # Assert
    assert len(found) == 1


# ---------------------------------------------------------------------------
# sibling_warning_lines — BOTH paths and BOTH mtimes, naming the outcome
# ---------------------------------------------------------------------------


def test_warning_lines_banner_names_the_agent(tmp_path: Path):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = tmp_path / "B" / ".scitex" / _TAIL
    # Act
    lines = sibling_warning_lines(loaded, siblings=[(sibling, _NEW)], agent="foo")
    # Assert
    assert "sac-drift WARNING for agent 'foo'" in _join(lines)


def test_warning_lines_name_the_loaded_path(tmp_path: Path):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = tmp_path / "B" / ".scitex" / _TAIL
    # Act
    lines = sibling_warning_lines(loaded, siblings=[(sibling, _NEW)], agent="foo")
    # Assert
    assert str(loaded) in _join(lines)


def test_warning_lines_name_the_sibling_path(tmp_path: Path):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = tmp_path / "B" / ".scitex" / _TAIL
    # Act
    lines = sibling_warning_lines(loaded, siblings=[(sibling, _NEW)], agent="foo")
    # Assert
    assert str(sibling) in _join(lines)


def test_warning_lines_carry_the_sibling_mtime(tmp_path: Path):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = tmp_path / "B" / ".scitex" / _TAIL
    # Act
    lines = sibling_warning_lines(loaded, siblings=[(sibling, _NEW)], agent="foo")
    # Assert
    assert f"mtime {_fmt(_NEW)}" in _join(lines)


def test_warning_lines_carry_the_loaded_mtime(tmp_path: Path):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = tmp_path / "B" / ".scitex" / _TAIL
    _touch(loaded, _OLD)
    # Act
    lines = sibling_warning_lines(loaded, siblings=[(sibling, _NEW)], agent="foo")
    # Assert
    assert f"mtime {_fmt(_OLD)}" in _join(lines)


def test_warning_lines_state_the_consequence(tmp_path: Path):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = tmp_path / "B" / ".scitex" / _TAIL
    # Act
    lines = sibling_warning_lines(loaded, siblings=[(sibling, _NEW)], agent="foo")
    # Assert
    assert "NOT in effect" in _join(lines)


# ---------------------------------------------------------------------------
# warn_if_newer_sibling — the launch-funnel entry point (advisory only)
# ---------------------------------------------------------------------------


def test_warn_emits_banner_to_explicit_stream(
    tmp_path: Path, isolated_scitex_dir, env_save_restore
):
    # Arrange — the operator points SAC_SPEC_SIBLING_ROOTS at the
    # source-tree parent; the sibling is newer than the loaded spec.
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _OLD)
    _touch(sibling, _NEW)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    stream = io.StringIO()
    # Act
    warn_if_newer_sibling(loaded, agent="foo", stream=stream)
    # Assert
    assert "sac-drift WARNING" in stream.getvalue()


def test_warn_returns_newer_sibling_count(
    tmp_path: Path, isolated_scitex_dir, env_save_restore
):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _OLD)
    _touch(sibling, _NEW)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    # Act
    count = warn_if_newer_sibling(loaded, agent="foo")
    # Assert
    assert count == 1


def test_warn_silent_without_newer_sibling(
    tmp_path: Path, isolated_scitex_dir, env_save_restore
):
    # Arrange — the sibling is OLDER: the normal state, no signal.
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _NEW)
    _touch(sibling, _OLD)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    stream = io.StringIO()
    # Act
    warn_if_newer_sibling(loaded, agent="foo", stream=stream)
    # Assert
    assert stream.getvalue() == ""


def test_warn_returns_zero_without_newer_sibling(
    tmp_path: Path, isolated_scitex_dir, env_save_restore
):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _NEW)
    _touch(sibling, _OLD)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    # Act
    count = warn_if_newer_sibling(loaded, agent="foo")
    # Assert
    assert count == 0


def test_warn_missing_spec_is_silent(tmp_path: Path, isolated_scitex_dir, env_save_restore):
    # Arrange — the loaded spec is absent; a sibling exists, but there is
    # nothing to compare against.
    _make_spec(tmp_path / "B")
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    loaded = tmp_path / "A" / ".scitex" / _TAIL
    # Act
    count = warn_if_newer_sibling(loaded, agent="foo")
    # Assert
    assert count == 0


def test_warn_ignores_unresolvable_roots(
    tmp_path: Path, isolated_scitex_dir, env_save_restore
):
    # Arrange — the env names a ghost root, a blank entry, and a root with
    # no spec: nothing to warn about, and nothing may raise.
    loaded = _make_spec(tmp_path / "A")
    env_save_restore.set(
        SIBLING_ROOTS_ENV,
        f"{tmp_path / 'ghost'}:{tmp_path / 'blank-adjacent'}:{tmp_path / 'B'}",
    )
    # Act
    count = warn_if_newer_sibling(loaded, agent="foo")
    # Assert
    assert count == 0


def test_unanchored_path_without_agent_is_a_noop(
    tmp_path: Path, isolated_scitex_dir, env_save_restore
):
    # Arrange — the loaded path has no .scitex anchor and no agent to
    # derive the canonical tail from: nothing to compare, silent no-op.
    spec = tmp_path / "loose" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("apiVersion: scitex-agent-container/v3\n")
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    # Act
    count = warn_if_newer_sibling(spec)
    # Assert
    assert count == 0


# ---------------------------------------------------------------------------
# The launch funnel — warn_if_spec_source_drifted must carry the warning
# ---------------------------------------------------------------------------


def test_strict_funnel_warns_on_newer_sibling(
    tmp_path: Path, isolated_scitex_dir, env_save_restore, capsys
):
    # Arrange — the 2026-08-15 incident shape: a NOT_A_REPO live spec
    # (the deliberate runtime layout) with a NEWER source-tree sibling.
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _OLD)
    _touch(sibling, _NEW)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    # Act
    warn_if_spec_source_drifted(loaded, agent="foo", strict=True, do_fetch=False)
    # Assert
    assert "sac-drift WARNING for agent 'foo'" in capsys.readouterr().err


def test_strict_funnel_names_the_inert_sibling(
    tmp_path: Path, isolated_scitex_dir, env_save_restore, capsys
):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _OLD)
    _touch(sibling, _NEW)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    # Act
    warn_if_spec_source_drifted(loaded, agent="foo", strict=True, do_fetch=False)
    # Assert
    assert str(sibling) in capsys.readouterr().err


def test_strict_funnel_sibling_warning_stays_a_warning(
    tmp_path: Path, isolated_scitex_dir, env_save_restore, capsys
):
    # Arrange
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _OLD)
    _touch(sibling, _NEW)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    # Act — must RETURN (not raise) even under strict; the sibling is a
    # warning, never a refusal.
    status = warn_if_spec_source_drifted(loaded, agent="foo", strict=True, do_fetch=False)
    # Assert
    assert status.state is DriftState.NOT_A_REPO


def test_strict_funnel_silent_on_older_sibling(
    tmp_path: Path, isolated_scitex_dir, env_save_restore, capsys
):
    # Arrange — the sibling is OLDER: the normal state, no sibling signal.
    loaded = _make_spec(tmp_path / "A")
    sibling = _make_spec(tmp_path / "B")
    _touch(loaded, _NEW)
    _touch(sibling, _OLD)
    env_save_restore.set(SIBLING_ROOTS_ENV, str(tmp_path / "B"))
    # Act
    warn_if_spec_source_drifted(loaded, agent="foo", strict=True, do_fetch=False)
    # Assert
    assert "sac-drift WARNING" not in capsys.readouterr().err
