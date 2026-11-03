"""Tests for ``_LazyCommandsDict`` enumeration truthfulness.

The CLI audit snapshots the tree by enumerating ``group.commands``
(``list()`` / ``len()`` / ``keys()``), not just by-name lookup. A lazy
dict that resolves on ``get``/``in`` but reports empty on enumeration
makes the audit believe commands are missing (false section-1a
violations on an otherwise-green tree).
"""

from __future__ import annotations

import click

from scitex_agent_container.cli_pkg._lazy_group import LazyGroup


def _probe_group() -> LazyGroup:
    class ProbeGroup(LazyGroup):
        # Resolvable specs (importlib targets that exist); enumeration
        # only asserts names/counts, never invokes callbacks.
        LAZY_COMMANDS = {
            "lazy-one": "click:Command",
            "lazy-two": "click:Group",
        }

    group = ProbeGroup(name="probe")

    @click.command()
    def real():
        """A real eager command."""

    group.add_command(real, "real")
    return group


def test_iteration_sees_lazy_entries():
    # Arrange
    group = _probe_group()
    # Act
    names = list(group.commands)
    # Assert
    assert set(names) == {"real", "lazy-one", "lazy-two"}


def test_len_counts_lazy_entries():
    # Arrange
    group = _probe_group()
    # Act / Assert
    assert len(group.commands) == 3


def test_keys_sees_lazy_entries():
    # Arrange
    group = _probe_group()
    # Act / Assert
    assert set(group.commands.keys()) == {"real", "lazy-one", "lazy-two"}


def test_unknown_names_stay_absent():
    # Arrange
    group = _probe_group()
    # Act / Assert
    assert "ghost" not in group.commands
    assert len(group.commands) == 3


def test_completion_style_extra_names_enumerated():
    # Arrange — a group resolving extra names outside LAZY_COMMANDS
    # (mirrors _MainGroup._COMPLETION_NAMES via its _resolve_lazy).
    import click

    from scitex_agent_container.cli_pkg._lazy_group import LazyGroup

    @click.command()
    def extra():
        """Attached on demand."""

    class ExtraGroup(LazyGroup):
        LAZY_COMMANDS = {}
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
