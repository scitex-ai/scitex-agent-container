"""Tests for ``runtimes.onboarding.ensure_project_onboarding``.

Pre-seeds ``~/.claude.json`` so Claude Code skips its per-workspace
onboarding wizard. We sandbox ``Path.home`` (autouse fixture pattern
adopted from ``tests/scitex_agent_container/_state/test_account_store.py``)
in addition to passing the ``home=`` override so a regression in
either layer cannot pollute the operator's real ``~/.claude.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.runtimes.onboarding import (
    _ONBOARDING_SEED,
    ensure_project_onboarding,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


def _load(home: Path) -> dict:
    return json.loads((home / ".claude.json").read_text())


class TestEnsureProjectOnboardingFreshInstall:
    def test_creates_file_and_returns_true(self, tmp_path):
        workdir = tmp_path / "ws"
        workdir.mkdir()
        assert ensure_project_onboarding(str(workdir), home=tmp_path) is True
        data = _load(tmp_path)
        entry = data["projects"][str(workdir.resolve())]
        assert entry["hasCompletedProjectOnboarding"] is True
        assert entry["hasTrustDialogAccepted"] is True

    def test_seed_fields_populated(self, tmp_path):
        workdir = tmp_path / "ws"
        workdir.mkdir()
        ensure_project_onboarding(str(workdir), home=tmp_path)
        entry = _load(tmp_path)["projects"][str(workdir.resolve())]
        for key in _ONBOARDING_SEED:
            assert key in entry

    def test_uses_default_home_when_omitted(self, tmp_path):
        # autouse _isolate_home points Path.home() → tmp_path
        workdir = tmp_path / "ws"
        workdir.mkdir()
        assert ensure_project_onboarding(str(workdir)) is True
        assert (tmp_path / ".claude.json").exists()

    def test_nonexistent_workdir_uses_unresolved_path(self, tmp_path):
        workdir = tmp_path / "does_not_exist"
        result = ensure_project_onboarding(str(workdir), home=tmp_path)
        assert result is True
        data = _load(tmp_path)
        # When workdir doesn't exist we keep the un-resolved expanded path
        assert str(workdir) in data["projects"]


class TestEnsureProjectOnboardingIdempotent:
    def test_already_complete_returns_false(self, tmp_path):
        workdir = tmp_path / "ws"
        workdir.mkdir()
        ensure_project_onboarding(str(workdir), home=tmp_path)
        # Second call should detect completion and bail out.
        assert ensure_project_onboarding(str(workdir), home=tmp_path) is False

    def test_preserves_existing_keys(self, tmp_path):
        workdir = tmp_path / "ws"
        workdir.mkdir()
        key = str(workdir.resolve())
        claude_json = tmp_path / ".claude.json"
        # Pre-seed with partial entry + live stats
        claude_json.write_text(
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
        assert ensure_project_onboarding(str(workdir), home=tmp_path) is True
        entry = _load(tmp_path)["projects"][key]
        # Live stats preserved
        assert entry["lastCost"] == 1.23
        assert entry["lastSessionId"] == "abc"
        # Existing allowedTools preserved (setdefault didn't overwrite)
        assert entry["allowedTools"] == ["MyTool"]
        # Critical fields set
        assert entry["hasCompletedProjectOnboarding"] is True
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasClaudeMdExternalIncludesApproved"] is True

    def test_force_overrides_false_completion_flag(self, tmp_path):
        workdir = tmp_path / "ws"
        workdir.mkdir()
        key = str(workdir.resolve())
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(
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
        assert ensure_project_onboarding(str(workdir), home=tmp_path) is True
        entry = _load(tmp_path)["projects"][key]
        assert entry["hasCompletedProjectOnboarding"] is True
        assert entry["hasTrustDialogAccepted"] is True


class TestEnsureProjectOnboardingErrorHandling:
    def test_malformed_json_returns_false(self, tmp_path, caplog):
        (tmp_path / ".claude.json").write_text("{not json")
        import logging

        with caplog.at_level(logging.WARNING):
            result = ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        assert result is False
        assert any("cannot read" in r.getMessage() for r in caplog.records)

    def test_unreadable_file_returns_false(self, tmp_path, monkeypatch, caplog):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text("{}")

        real_open = Path.open

        def _fake_open(self, *args, **kwargs):
            if self == claude_json and "r" in (
                args[0] if args else kwargs.get("mode", "r")
            ):
                raise OSError("simulated read failure")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _fake_open)
        import logging

        with caplog.at_level(logging.WARNING):
            result = ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        assert result is False

    def test_write_failure_returns_false(self, tmp_path, monkeypatch, caplog):
        from scitex_agent_container.runtimes import onboarding as ob

        def _boom(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(ob.os, "replace", _boom)
        import logging

        with caplog.at_level(logging.WARNING):
            result = ensure_project_onboarding(str(tmp_path / "ws"), home=tmp_path)
        assert result is False
        assert any("cannot write" in r.getMessage() for r in caplog.records)
        # tmp file should be cleaned up
        assert not (tmp_path / ".claude.json.tmp").exists()


class TestPreservesOtherProjects:
    def test_other_project_entries_unaffected(self, tmp_path):
        other_key = "/some/other/workdir"
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(
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
        workdir = tmp_path / "ws"
        workdir.mkdir()
        assert ensure_project_onboarding(str(workdir), home=tmp_path) is True
        data = _load(tmp_path)
        assert data["projects"][other_key]["lastCost"] == 9.99
        assert str(workdir.resolve()) in data["projects"]

    def test_top_level_keys_preserved(self, tmp_path):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(
            json.dumps(
                {
                    "telemetry": {"enabled": False},
                    "version": "1.2.3",
                }
            )
        )
        workdir = tmp_path / "ws"
        workdir.mkdir()
        ensure_project_onboarding(str(workdir), home=tmp_path)
        data = _load(tmp_path)
        assert data["telemetry"] == {"enabled": False}
        assert data["version"] == "1.2.3"
