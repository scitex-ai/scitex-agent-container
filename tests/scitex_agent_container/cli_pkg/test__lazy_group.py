"""Tests for ``_LazyCommandsDict`` enumeration truthfulness (scoped).

The CLI audit snapshots the tree by enumerating ``group.commands``
(``list()`` / ``len()`` / ``keys()``), not just by-name lookup. A lazy
dict that resolves on ``get``/``in`` but reports empty on enumeration
makes the audit believe commands are missing (false section-1a
violations on an otherwise-green tree — SAC PR #1726: 20,909 passed,
1 failed on the missing list-python-apis/mcp-list-tools probe).

Enumeration is deliberately SCOPED to ``_ENUMERATE_EAGER_NAMES``:
materialising every lazy entry would drag each subcommand module
into the process and break the ``--help`` import closure the
startup-budget test guards. ``list-python-apis`` and ``mcp`` stay
visible because they are audit-required; shell-completion names come
via the group's ``_COMPLETION_NAMES`` extras.
"""

from __future__ import annotations

import click

from scitex_agent_container.cli_pkg._lazy_group import LazyGroup


def test_scoped_names_enumerated_by_default():
    # Arrange — group carrying the audit-required lazy names.
    class ProbeGroup(LazyGroup):
        LAZY_COMMANDS = {
            "list-python-apis": "click:Command",
            "mcp": "click:Group",
        }

    group = ProbeGroup(name="probe")
    # Act
    names = list(group.commands)
    # Assert — the audit-required names resolve without full population.
    assert "list-python-apis" in names
    assert "mcp" in names


def test_len_matches_list():
    # Arrange
    group = LazyGroup(name="probe")
    # Act / Assert
    assert len(group.commands) == len(list(group.commands))


def test_keys_match_list():
    # Arrange
    group = LazyGroup(name="probe")
    # Act / Assert
    assert set(group.commands.keys()) == set(list(group.commands))


def test_unknown_names_stay_absent():
    # Arrange
    group = LazyGroup(name="probe")
    # Act / Assert
    assert "ghost" not in group.commands


def test_bypopulation_does_not_import_lazy_modules():
    # Arrange — a lazy name whose module does not exist: enumeration
    # must not attempt the import (scoped names only).
    class ProbeGroup(LazyGroup):
        LAZY_COMMANDS = {
            "lazy-ghost": "no_such_pkg_xyz:Thing",
        }

    group = ProbeGroup(name="probe")
    # Act
    names = list(group.commands)
    # Assert
    assert "lazy-ghost" not in names


def test_by_name_resolves_anything_on_demand():
    # Arrange
    @click.command()
    def real():
        """A real eager command."""

    group = LazyGroup(name="probe")
    group.add_command(real, "real")
    # Act — by-name access resolves beyond the enumeration scope.
    assert group.commands.get("real") is real
    assert len(group.commands) >= 1


def test_completion_extras_enumerated():
    # Arrange — mirrors _MainGroup._COMPLETION_NAMES via _resolve_lazy.
    @click.command()
    def extra():
        """Attached on demand."""

    class ExtraGroup(LazyGroup):
        _COMPLETION_NAMES = ("extra-cmd",)

        def _resolve_lazy(self, name):
            if name == "extra-cmd":
                self.commands["extra-cmd"] = extra
                return extra
            return super()._resolve_lazy(name)

    group = ExtraGroup(name="probe")
    # Act
    names = list(group.commands)
    # Assert
    assert "extra-cmd" in names


def test_populate_all_imports_stay_scoped():
    # Arrange — importing the dict must not pull lazy subcommand modules.
    import sys

    before = {m for m in sys.modules if m.startswith("scitex_agent_container.")}
    group = LazyGroup(name="probe")
    # Act
    list(group.commands)
    after = {m for m in sys.modules if m.startswith("scitex_agent_container.")}
    # Assert — only cheap already-loaded modules, no new lazy imports.
    assert after - before == set()
