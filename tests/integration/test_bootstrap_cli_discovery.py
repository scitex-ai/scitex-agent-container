"""Console discovery retains lazy commands without executing the wrapper."""

from contextlib import contextmanager

import click
import pytest

from _scitex_agent_container_bootstrap import _load_cli


def test_bootstrap_lists_public_lazy_commands_without_importing_handlers():
    # Arrange: importing any handler during name discovery is a failure.
    from scitex_agent_container.cli import main

    def refuse_handler_import(_spec):
        raise AssertionError("name discovery imported a command handler")

    # Act: use Click's public discovery protocol, as external auditors do.
    with _temporary_attribute(main, "_import", refuse_handler_import):
        facade = _load_cli()
        names = facade.list_commands(click.Context(facade))

    # Assert: required interfaces are discoverable; renamed aliases stay hidden.
    assert ("list-python-apis" in names, "mcp" in names, "start" in names) == (
        True,
        True,
        False,
    )


@pytest.mark.parametrize("name", ["list-python-apis", "mcp"])
def test_bootstrap_resolves_discovered_command_without_invoking_it(name):
    # Arrange: the actual console facade and its real underlying command group.
    from scitex_agent_container.cli import main

    facade = _load_cli()
    context = click.Context(facade)

    # Act: resolve a handler, without running its callback or console wrapper.
    observed = facade.get_command(context, name)

    # Assert: callers receive the real registered command object.
    assert observed is not None and observed is main.get_command(context, name)


def test_bootstrap_preserves_custom_group_lookup_without_executing_cli():
    # Arrange: a valid Click group with a public lookup beyond its command dict.
    from scitex_agent_container import cli

    command = click.Command("dynamic")

    class DynamicGroup(click.Group):
        def get_command(self, ctx, cmd_name):
            return command if cmd_name == "dynamic" else None

    def refuse_execution():
        raise AssertionError("discovery invoked the console wrapper")

    # Act: resolve through the facade's public Click interface.
    with (
        _temporary_attribute(cli, "main", DynamicGroup("fixture")),
        _temporary_attribute(cli, "cli_entry_point", refuse_execution),
    ):
        facade = _load_cli()
        observed = facade.get_command(click.Context(facade), "dynamic")

    # Assert: custom lookup is retained without executing a callback.
    assert observed is command


@contextmanager
def _temporary_attribute(owner, name, value):
    """Restore exact object state around an inert component fixture."""
    absent = object()
    previous = vars(owner).get(name, absent)
    setattr(owner, name, value)
    try:
        yield
    finally:
        if previous is absent:
            delattr(owner, name)
        else:
            setattr(owner, name, previous)


@pytest.mark.parametrize(
    "canonical,legacy",
    [("list-ports", "ports"), ("list-versions", "versions"),
     ("show-provenance", "provenance")],
)
def test_read_actions_are_discoverable_while_noun_aliases_stay_hidden(
    canonical, legacy
):
    # Arrange
    facade = _load_cli()
    # Act
    names = facade.list_commands(click.Context(facade))
    # Assert
    assert (canonical in names, legacy in names) == (True, False)


@pytest.mark.parametrize(
    "canonical,legacy",
    [("list-ports", "ports"), ("list-versions", "versions"),
     ("show-provenance", "provenance")],
)
def test_legacy_read_names_resolve_to_the_original_callback(canonical, legacy):
    # Arrange
    facade = _load_cli()
    ctx = click.Context(facade)
    # Act
    current = facade.get_command(ctx, canonical)
    old = facade.get_command(ctx, legacy)
    # Assert
    assert current is not None and old is not None and current.callback is old.callback
