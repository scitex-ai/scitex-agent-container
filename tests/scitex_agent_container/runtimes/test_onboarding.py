"""Tests for ``runtimes.onboarding.ensure_project_onboarding``.

Pre-seeds ``~/.claude.json`` so Claude Code skips its per-workspace
onboarding wizard. We sandbox ``Path.home`` (autouse fixture pattern
adopted from ``tests/scitex_agent_container/_state/test_account_store.py``)
in addition to passing the ``home=`` override so a regression in
either layer cannot pollute the operator's real ``~/.claude.json``.

TQ cleanup: every test carries AAA markers (TQ002), descriptive names
spell out the behaviour being verified (TQ003), and each test asserts
exactly one fact (TQ007). Same-shape invariants collapse into
``pytest.parametrize`` so the matrix stays declarative.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from scitex_agent_container.runtimes.onboarding import (
    _ONBOARDING_SEED,
    ensure_project_onboarding,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path):
    """PA-306: $HOME save/restore — Path.home() reads $HOME on Unix."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _load(home: Path) -> dict:
    return json.loads((home / ".claude.json").read_text())


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Workspace directory that exists on disk."""
    wd = tmp_path / "ws"
    wd.mkdir()
    return wd


@pytest.fixture
def fresh_install_entry(tmp_path: Path, workdir: Path) -> dict:
    """Run ensure_project_onboarding once on a fresh home and return the entry."""
    ensure_project_onboarding(str(workdir), home=tmp_path)
    return _load(tmp_path)["projects"][str(workdir.resolve())]


# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------


class TestEnsureProjectOnboardingFreshInstall:
    def test_fresh_install_returns_true_on_first_call(self, tmp_path, workdir):
        # Arrange
        workdir_str = str(workdir)
        # Act
        result = ensure_project_onboarding(workdir_str, home=tmp_path)
        # Assert
        assert result is True

    def test_fresh_install_creates_claude_json_file_on_disk(self, tmp_path, workdir):
        # Arrange
        workdir_str = str(workdir)
        # Act
        ensure_project_onboarding(workdir_str, home=tmp_path)
        # Assert
        assert (tmp_path / ".claude.json").exists()

    @pytest.mark.parametrize(
        "field",
        [
            "hasCompletedProjectOnboarding",
            "hasTrustDialogAccepted",
        ],
    )
    def test_fresh_install_sets_critical_gate_flag_true(
        self, fresh_install_entry, field
    ):
        # Arrange
        entry = fresh_install_entry
        # Act
        value = entry[field]
        # Assert
        assert value is True

    @pytest.mark.parametrize("seed_key", list(_ONBOARDING_SEED.keys()))
    def test_fresh_install_populates_every_seed_field(
        self, fresh_install_entry, seed_key
    ):
        # Arrange
        entry = fresh_install_entry
        # Act
        present = seed_key in entry
        # Assert
        assert present

    def test_fresh_install_uses_default_home_when_home_omitted(self, tmp_path, workdir):
        # Arrange — autouse _isolate_home points Path.home() → tmp_path
        # Act
        result = ensure_project_onboarding(str(workdir))
        # Assert
        assert result is True

    def test_default_home_writes_claude_json_at_isolated_home(self, tmp_path, workdir):
        # Arrange
        workdir_str = str(workdir)
        # Act
        ensure_project_onboarding(workdir_str)
        # Assert
        assert (tmp_path / ".claude.json").exists()

    def test_nonexistent_workdir_returns_true(self, tmp_path):
        # Arrange
        missing = tmp_path / "does_not_exist"
        # Act
        result = ensure_project_onboarding(str(missing), home=tmp_path)
        # Assert
        assert result is True

    def test_nonexistent_workdir_keeps_unresolved_expanded_path_as_key(self, tmp_path):
        # Arrange
        missing = tmp_path / "does_not_exist"
        # Act
        ensure_project_onboarding(str(missing), home=tmp_path)
        # Assert
        assert str(missing) in _load(tmp_path)["projects"]


# ---------------------------------------------------------------------------
# Idempotent / merge behaviour
# ---------------------------------------------------------------------------


class TestEnsureProjectOnboardingIdempotent:
    def test_second_call_on_already_complete_entry_returns_false(
        self, tmp_path, workdir
    ):
        # Arrange
        ensure_project_onboarding(str(workdir), home=tmp_path)
        # Act
        result = ensure_project_onboarding(str(workdir), home=tmp_path)
        # Assert
        assert result is False

    @pytest.fixture
    def merged_entry_with_preexisting_live_stats(
        self, tmp_path: Path, workdir: Path
    ) -> dict:
        """Seed partial entry + live stats, then run ensure once."""
        key = str(workdir.resolve())
        (tmp_path / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        key: {
                            "lastCost": 1.23,
                            "lastSessionId": "abc",
                            "allowedTools": ["MyTool"],
                        }
                    }
                }
            )
        )
        ensure_project_onboarding(str(workdir), home=tmp_path)
        return _load(tmp_path)["projects"][key]

    def test_merge_returns_true_when_completion_flag_missing(self, tmp_path, workdir):
        # Arrange
        key = str(workdir.resolve())
        (tmp_path / ".claude.json").write_text(
            json.dumps({"projects": {key: {"lastCost": 1.23}}})
        )
        # Act
        result = ensure_project_onboarding(str(workdir), home=tmp_path)
        # Assert
        assert result is True

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("lastCost", 1.23),
            ("lastSessionId", "abc"),
            ("allowedTools", ["MyTool"]),
        ],
    )
    def test_merge_preserves_preexisting_field_value(
        self, merged_entry_with_preexisting_live_stats, field, expected
    ):
        # Arrange
        entry = merged_entry_with_preexisting_live_stats
        # Act
        value = entry[field]
        # Assert
        assert value == expected

    @pytest.mark.parametrize(
        "field",
        [
            "hasCompletedProjectOnboarding",
            "hasTrustDialogAccepted",
            "hasClaudeMdExternalIncludesApproved",
        ],
    )
    def test_merge_sets_critical_gate_flag_true(
        self, merged_entry_with_preexisting_live_stats, field
    ):
        # Arrange
        entry = merged_entry_with_preexisting_live_stats
        # Act
        value = entry[field]
        # Assert
        assert value is True

    @pytest.fixture
    def force_overridden_entry(self, tmp_path: Path, workdir: Path) -> dict:
        """Seed entry with completion flags set to False, run ensure once."""
        key = str(workdir.resolve())
        (tmp_path / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        key: {
                            "hasCompletedProjectOnboarding": False,
                            "hasTrustDialogAccepted": False,
                        }
                    }
                }
            )
        )
        ensure_project_onboarding(str(workdir), home=tmp_path)
        return _load(tmp_path)["projects"][key]

    @pytest.mark.parametrize(
        "field",
        [
            "hasCompletedProjectOnboarding",
            "hasTrustDialogAccepted",
        ],
    )
    def test_force_overrides_false_completion_flag_to_true(
        self, force_overridden_entry, field
    ):
        # Arrange
        entry = force_overridden_entry
        # Act
        value = entry[field]
        # Assert
        assert value is True


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestEnsureProjectOnboardingErrorHandling:
    def test_malformed_json_file_causes_function_to_return_false(self, tmp_path):
        # Arrange
        (tmp_path / ".claude.json").write_text("{not json")
        # Act
        result = ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        # Assert
        assert result is False

    def test_malformed_json_file_emits_cannot_read_warning_log(self, tmp_path, caplog):
        # Arrange
        (tmp_path / ".claude.json").write_text("{not json")
        # Act
        with caplog.at_level(logging.WARNING):
            ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        # Assert
        assert any("cannot read" in r.getMessage() for r in caplog.records)

    def test_unreadable_claude_json_causes_function_to_return_false(self, tmp_path):
        # Arrange
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text("{}")
        real_open = Path.open

        def _fake_open(self, *args, **kwargs):
            if self == claude_json and "r" in (
                args[0] if args else kwargs.get("mode", "r")
            ):
                raise OSError("simulated read failure")
            return real_open(self, *args, **kwargs)

        # PA-306: save/restore Path.open directly.
        saved_open = Path.open
        Path.open = _fake_open  # type: ignore[assignment]
        try:
            # Act
            result = ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        finally:
            Path.open = saved_open  # type: ignore[assignment]
        # Assert
        assert result is False

    def test_write_failure_causes_function_to_return_false(self, tmp_path):
        # Arrange
        from scitex_agent_container.runtimes import onboarding as ob

        def _boom(src, dst):
            raise OSError("simulated replace failure")

        saved_replace = ob.os.replace
        ob.os.replace = _boom  # type: ignore[assignment]
        try:
            # Act
            result = ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        finally:
            ob.os.replace = saved_replace  # type: ignore[assignment]
        # Assert
        assert result is False

    def test_write_failure_emits_cannot_write_warning_log(self, tmp_path, caplog):
        # Arrange
        from scitex_agent_container.runtimes import onboarding as ob

        def _boom(src, dst):
            raise OSError("simulated replace failure")

        saved_replace = ob.os.replace
        ob.os.replace = _boom  # type: ignore[assignment]
        try:
            # Act
            with caplog.at_level(logging.WARNING):
                ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        finally:
            ob.os.replace = saved_replace  # type: ignore[assignment]
        # Assert
        assert any("cannot write" in r.getMessage() for r in caplog.records)

    def test_write_failure_cleans_up_temporary_dot_tmp_file(self, tmp_path):
        # Arrange
        from scitex_agent_container.runtimes import onboarding as ob

        def _boom(src, dst):
            raise OSError("simulated replace failure")

        saved_replace = ob.os.replace
        ob.os.replace = _boom  # type: ignore[assignment]
        try:
            # Act
            ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        finally:
            ob.os.replace = saved_replace  # type: ignore[assignment]
        # Assert
        assert not (tmp_path / ".claude.json.tmp").exists()


# ---------------------------------------------------------------------------
# Preserves other projects + top-level keys
# ---------------------------------------------------------------------------


class TestEnsureProjectOnboardingPreservesOtherProjects:
    @pytest.fixture
    def state_after_seeding_other_project(self, tmp_path: Path, workdir: Path) -> dict:
        """Seed unrelated project, run ensure on workdir, return loaded data."""
        other_key = "/some/other/workdir"
        (tmp_path / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        other_key: {
                            "hasCompletedProjectOnboarding": True,
                            "lastCost": 9.99,
                        }
                    }
                }
            )
        )
        ensure_project_onboarding(str(workdir), home=tmp_path)
        return _load(tmp_path)

    def test_unrelated_project_entry_preserves_its_lastcost(
        self, state_after_seeding_other_project
    ):
        # Arrange
        data = state_after_seeding_other_project
        # Act
        last_cost = data["projects"]["/some/other/workdir"]["lastCost"]
        # Assert
        assert last_cost == 9.99

    def test_target_workdir_entry_added_alongside_unrelated_project(
        self, state_after_seeding_other_project, workdir
    ):
        # Arrange
        data = state_after_seeding_other_project
        # Act
        keys = data["projects"].keys()
        # Assert
        assert str(workdir.resolve()) in keys

    @pytest.fixture
    def state_after_seeding_top_level_keys(self, tmp_path: Path, workdir: Path) -> dict:
        """Seed top-level keys, run ensure, return loaded data."""
        (tmp_path / ".claude.json").write_text(
            json.dumps(
                {
                    "telemetry": {"enabled": False},
                    "version": "1.2.3",
                }
            )
        )
        ensure_project_onboarding(str(workdir), home=tmp_path)
        return _load(tmp_path)

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("telemetry", {"enabled": False}),
            ("version", "1.2.3"),
        ],
    )
    def test_top_level_field_preserved_after_ensure(
        self, state_after_seeding_top_level_keys, field, expected
    ):
        # Arrange
        data = state_after_seeding_top_level_keys
        # Act
        value = data[field]
        # Assert
        assert value == expected
