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

import pytest

from scitex_agent_container.runtimes._to_home_errors import UnknownToHomeLayer
from scitex_agent_container.runtimes._to_home_resolve import (
    _user_baseline_to_home_dir,
    settings_layer_dirs,
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


class _DeclaringSpec:
    """The four attributes ``settings_layer_dirs`` reads off a config.

    Not a mock of a collaborator — the resolver takes a config OBJECT and reads
    exactly these, and every path handed to it below is a real directory.
    """

    def __init__(self, to_home_layers, to_home):
        self.name = "agent-under-test"
        self.to_home_layers = to_home_layers
        self.to_home = to_home
        self.config_path = ""


def _resolved_names(layers):
    """Map layer name -> whether that layer resolved to a directory."""
    return {name: path is not None for name, path in layers}


class TestToHomeLayerDeclaration:
    """``spec.to_home_layers``: the spec states which cascade layers it
    inherits, so what gets merged in is visible from the spec alone instead of
    discovered on disk. Absent keeps today's behaviour (all 102 registered
    specs are currently in that state); a misspelt name is refused, because
    ignoring it would silently inherit nothing."""

    def _layers(self, tmp_path, env_save_restore):
        user = tmp_path / "user-shared" / "to_home" / ".claude"
        user.mkdir(parents=True)
        agent = tmp_path / "agent" / "to_home" / ".claude"
        agent.mkdir(parents=True)
        env_save_restore.set("SAC_USER_TO_HOME_BASELINE", str(user.parent))
        return str(agent.parent)

    def test_absent_declaration_keeps_inheriting_the_user_layer(
        self, tmp_path, env_save_restore
    ):
        # Arrange — an undeclared spec must behave exactly as it does today.
        agent_to_home = self._layers(tmp_path, env_save_restore)
        spec = _DeclaringSpec(None, agent_to_home)
        # Act
        resolved = _resolved_names(settings_layer_dirs(spec))
        # Assert
        assert resolved["user-shared"] is True

    def test_absent_declaration_keeps_inheriting_the_agent_layer(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        agent_to_home = self._layers(tmp_path, env_save_restore)
        spec = _DeclaringSpec(None, agent_to_home)
        # Act
        resolved = _resolved_names(settings_layer_dirs(spec))
        # Assert
        assert resolved["per-agent"] is True

    def test_undeclared_layer_is_dropped(self, tmp_path, env_save_restore):
        # Arrange — the point of the field: declaring one layer excludes others.
        agent_to_home = self._layers(tmp_path, env_save_restore)
        spec = _DeclaringSpec(["per-agent"], agent_to_home)
        # Act
        resolved = _resolved_names(settings_layer_dirs(spec))
        # Assert
        assert resolved["user-shared"] is False

    def test_declared_layer_is_kept(self, tmp_path, env_save_restore):
        # Arrange
        agent_to_home = self._layers(tmp_path, env_save_restore)
        spec = _DeclaringSpec(["per-agent"], agent_to_home)
        # Act
        resolved = _resolved_names(settings_layer_dirs(spec))
        # Assert
        assert resolved["per-agent"] is True

    def test_empty_declaration_inherits_nothing(self, tmp_path, env_save_restore):
        # Arrange — an explicit [] is a spec pinning itself, NOT "absent".
        agent_to_home = self._layers(tmp_path, env_save_restore)
        spec = _DeclaringSpec([], agent_to_home)
        # Act
        resolved = _resolved_names(settings_layer_dirs(spec))
        # Assert
        assert not any(resolved.values())

    def test_misspelt_layer_is_refused(self, tmp_path, env_save_restore):
        # Arrange — "user_shared" matches no layer; ignoring it inherits nothing.
        agent_to_home = self._layers(tmp_path, env_save_restore)
        spec = _DeclaringSpec(["user_shared"], agent_to_home)
        # Act
        # Assert
        with pytest.raises(UnknownToHomeLayer, match="user_shared"):
            settings_layer_dirs(spec)

    def test_order_follows_precedence_not_declaration_order(
        self, tmp_path, env_save_restore
    ):
        # Arrange — declared "backwards"; precedence must stay lowest-first.
        agent_to_home = self._layers(tmp_path, env_save_restore)
        spec = _DeclaringSpec(["per-agent", "user-shared"], agent_to_home)
        # Act
        order = [name for name, _ in settings_layer_dirs(spec)]
        # Assert
        assert order == ["user-shared", "project-shared", "per-agent"]


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
