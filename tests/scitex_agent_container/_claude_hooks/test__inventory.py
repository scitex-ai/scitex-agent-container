"""Tests for the in-container hook inventory.

THE FACT THIS FILE EXISTS FOR: a single-layer read and the effective view
DISAGREE, and the check must report the EFFECTIVE one. Measured 2026-08-10 —
layer 1 said 67 pre-tool-use hooks and called ``log_post_tool_use.sh``
missing; the same listing inside the container returned 71 and the hook was
there. Every test below that touches those numbers would have PASSED against a
layer-only implementation for the wrong reason, so they assert the DISAGREEMENT
first and the resolution second.

PA-306 no-mocks: real directories, real files, real ``$HOME``. ``Path.home()``
reads ``$HOME`` on POSIX, so pointing the env at a tmp tree exercises the exact
production code path with no patching.
STX-TQ002/TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container._claude_hooks._inventory import (
    HOOK_EVENT_DIRS,
    inventory_hooks,
)

from ._trees import (
    EFFECTIVE_PRE_COUNT,
    LAYER_PRE_COUNT,
    effective_home,
    layer_only_home,
    write_hooks,
)

_MISSED_HOOK = ("post-tool-use", "log_post_tool_use.sh")


class TestTheSingleLayerViewDisagreesWithTheEffectiveOne:
    """The 67-vs-71 discrepancy, encoded so it cannot silently come back."""

    def test_layer_only_view_undercounts_pre_tool_use(self, tmp_path: Path):
        # Arrange
        home = layer_only_home(tmp_path)
        # Act
        inv = inventory_hooks(home=home)
        # Assert
        assert inv.counts["pre-tool-use"] == LAYER_PRE_COUNT

    def test_effective_view_counts_more_than_the_layer(self, tmp_path: Path):
        # Arrange
        home = effective_home(tmp_path)
        # Act
        inv = inventory_hooks(home=home)
        # Assert
        assert inv.counts["pre-tool-use"] == EFFECTIVE_PRE_COUNT

    def test_the_two_views_genuinely_disagree(self, tmp_path: Path):
        # Arrange — the control. If these ever match, every other test in this
        # class is passing for a reason that no longer exists.
        layer = inventory_hooks(home=layer_only_home(tmp_path))
        effective = inventory_hooks(home=effective_home(tmp_path))
        # Act
        same = layer.counts == effective.counts
        # Assert
        assert same is False

    def test_layer_only_view_calls_the_present_hook_missing(self, tmp_path: Path):
        # Arrange — the concrete harm: not "a number differs" but "a named
        # guarantee is reported absent while it is armed".
        home = layer_only_home(tmp_path)
        # Act
        armed = inventory_hooks(home=home).has(*_MISSED_HOOK)
        # Assert
        assert armed is False

    def test_effective_view_finds_the_hook_the_layer_missed(self, tmp_path: Path):
        # Arrange
        home = effective_home(tmp_path)
        # Act
        armed = inventory_hooks(home=home).has(*_MISSED_HOOK)
        # Assert
        assert armed is True


class TestTheDefaultReadIsTheEffectiveOne:
    """``inventory_hooks()`` with no argument — what the gate actually calls —
    must read ``$HOME``, i.e. the container's own resolved mount stack."""

    def test_no_argument_read_uses_the_process_home(
        self, tmp_path: Path, env_save_restore
    ):
        # Arrange
        env_save_restore.set("HOME", str(effective_home(tmp_path)))
        # Act
        inv = inventory_hooks()
        # Assert
        assert inv.counts["pre-tool-use"] == EFFECTIVE_PRE_COUNT

    def test_no_argument_read_is_not_the_layer_count(
        self, tmp_path: Path, env_save_restore
    ):
        # Arrange — the negative half of the same fact, stated separately so a
        # future "effective == layer" fixture bug cannot hide behind it.
        env_save_restore.set("HOME", str(effective_home(tmp_path)))
        # Act
        inv = inventory_hooks()
        # Assert
        assert inv.counts["pre-tool-use"] != LAYER_PRE_COUNT


class TestWhatCountsAsAHook:
    """Same rules as the host-merge walk, so the two cannot disagree about
    what a hook IS while agreeing about where hooks live."""

    def test_markdown_docs_are_not_hooks(self, tmp_path: Path):
        # Arrange
        home = write_hooks(tmp_path / "h", {"pre-tool-use": ["real.sh", "README.md"]})
        # Act
        inv = inventory_hooks(home=home)
        # Assert
        assert inv.dirs["pre-tool-use"] == ["real.sh"]

    def test_run_log_artifacts_are_not_hooks(self, tmp_path: Path):
        # Arrange — hooks write ``.<script>.sh.log`` beside themselves.
        home = write_hooks(tmp_path / "h", {"pre-tool-use": ["real.sh"]})
        (home / ".claude/hooks/pre-tool-use/.real.sh.log").write_text("ran\n")
        # Act
        inv = inventory_hooks(home=home)
        # Assert
        assert inv.dirs["pre-tool-use"] == ["real.sh"]

    def test_subdirectories_are_not_hooks(self, tmp_path: Path):
        # Arrange
        home = write_hooks(tmp_path / "h", {"pre-tool-use": ["real.sh"]})
        (home / ".claude/hooks/pre-tool-use/__pycache__").mkdir()
        # Act
        inv = inventory_hooks(home=home)
        # Assert
        assert inv.dirs["pre-tool-use"] == ["real.sh"]

    def test_every_known_event_dir_is_surveyed(self, tmp_path: Path):
        # Arrange — an empty home: nothing armed, but every event dir accounted
        # for, so a NEW event dir added to the host-merge constant is surveyed
        # here automatically rather than being silently unmeasured.
        home = tmp_path / "empty"
        (home / ".claude" / "hooks").mkdir(parents=True)
        # Act
        inv = inventory_hooks(home=home)
        # Assert
        assert set(inv.missing_dirs) == set(HOOK_EVENT_DIRS)


class TestAbsenceIsNotTheSameAsIgnorance:
    """An empty inventory and an unreadable one look identical to a caller and
    mean opposite things, so they are never allowed to produce the same value."""

    def test_absent_event_dir_is_a_definite_no(self, tmp_path: Path):
        # Arrange — readable root, directory simply not there.
        home = write_hooks(tmp_path / "h", {"pre-tool-use": ["real.sh"]})
        # Act
        armed = inventory_hooks(home=home).has("stop", "anything.sh")
        # Assert
        assert armed is False

    def test_unreadable_root_is_unknown_not_absent(self, tmp_path: Path):
        # Arrange — no .claude/hooks at all (e.g. a host shell, or a mount that
        # is not there yet).
        home = tmp_path / "no-such-home"
        home.mkdir()
        # Act
        armed = inventory_hooks(home=home).has("pre-tool-use", "anything.sh")
        # Assert
        assert armed is None

    def test_unreadable_root_is_reported_not_swallowed(self, tmp_path: Path):
        # Arrange
        home = tmp_path / "no-such-home"
        home.mkdir()
        # Act
        inv = inventory_hooks(home=home)
        # Assert
        assert inv.root_error is not None

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root ignores mode 000; the fixture cannot deny it"
    )
    def test_unlistable_event_dir_is_unknown(self, tmp_path: Path):
        # Arrange — a real, unreadable directory (mode 000). Root stays fine,
        # so this isolates per-directory ignorance from whole-root ignorance.
        home = write_hooks(tmp_path / "h", {"pre-tool-use": ["real.sh"]})
        locked = home / ".claude" / "hooks" / "stop"
        locked.mkdir()
        locked.chmod(0o000)
        try:
            # Act
            armed = inventory_hooks(home=home).has("stop", "anything.sh")
        finally:
            locked.chmod(0o755)
        # Assert
        assert armed is None
