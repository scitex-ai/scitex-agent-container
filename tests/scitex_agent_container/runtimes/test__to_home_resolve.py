"""Tests for the to_home path-resolution helpers (``_to_home_resolve``).

Focused on ``_user_baseline_to_home_dir``'s ``$SAC_USER_TO_HOME_BASELINE``
override / opt-out — the escape hatch that lets a sandboxed agent pin its
home by spec alone (mirrors ``resolve_baseline_to_home_dir``'s
``$SAC_TO_HOME_BASELINE``). Also pins the ``_to_home`` re-export contract so
the split from ``_to_home`` stays import-compatible.

PA-306 no-mocks: real paths against ``tmp_path``; env-driven tests use the
project-wide ``env_save_restore`` fixture (POSIX-honest ``setenv``/``delenv``).
STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._to_home_resolve import (
    _user_baseline_to_home_dir,
)


class TestUserBaselineOptOut:
    """``$SAC_USER_TO_HOME_BASELINE``: a dir overrides the ``~`` default; a
    NON-dir opts the user layer OUT so a sandboxed solver's home is
    spec-pinned (arm-consistency across benchmark runs)."""

    def test_env_override_takes_precedence(self, tmp_path, env_save_restore):
        # Arrange — an explicit user baseline dir wins over the ~ default.
        custom = tmp_path / "user_baseline" / "to_home"
        custom.mkdir(parents=True)
        env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(custom))
        # Act
        resolved = _user_baseline_to_home_dir()
        # Assert
        assert resolved == custom

    def test_env_set_to_missing_dir_opts_out_returns_none(
        self, tmp_path, env_save_restore
    ):
        # Arrange — set to a non-dir ⇒ user baseline absent (the opt-out).
        env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(tmp_path / "nope"))
        # Act
        resolved = _user_baseline_to_home_dir()
        # Assert
        assert resolved is None


class TestToHomeReExportContract:
    """The resolvers moved to ``_to_home_resolve`` but ``_to_home`` must keep
    re-exporting them (legacy import path + ``sac agents explain``)."""

    def test_to_home_reexports_resolvers(self):
        # Arrange
        from scitex_agent_container.runtimes import _to_home
        # Act
        names = [
            _to_home.resolve_to_home_dir,
            _to_home.resolve_baseline_to_home_dir,
            _to_home.settings_layer_dirs,
            _to_home._user_baseline_to_home_dir,
        ]
        # Assert
        assert all(callable(n) for n in names)
