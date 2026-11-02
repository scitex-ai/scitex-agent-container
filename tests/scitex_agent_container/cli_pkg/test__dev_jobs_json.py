"""Structured status forwarding uses the resolved command's actual capability."""

from __future__ import annotations

import click
import pytest

from scitex_agent_container.cli_pkg import _dev_jobs_backend as backend
from scitex_agent_container.cli_pkg._dev_jobs import _make_group


@pytest.mark.parametrize("kind", ["service", "timer"])
def test_status_json_reaches_the_public_dev_command(kind):
    # Arrange
    delegation = backend.resolve(kind, "status")
    target = backend.leaf_command(delegation.path, "status")
    # Act
    argv = backend.build_argv(
        delegation, name="owned-example", yes=False, as_json=True,
        leaf=target, exe="scitex-dev",
    )
    context = target.make_context("status", argv[5:])
    # Assert
    assert (context.params["name"], context.params["as_json"]) == ("owned-example", True)


def test_legacy_status_without_json_refuses_before_execution():
    # Arrange
    delegation = backend.Delegation(
        ("systemd",), "status", True, "owned legacy Click command"
    )
    target = click.Command("status", params=[click.Argument(["name"])])
    # Act
    try:
        backend.build_argv(
            delegation, name="owned-example", yes=False, as_json=True, leaf=target
        )
    except click.ClickException as exc:
        refusal = "does not support --json" in str(exc)
    else:
        refusal = False
    # Assert
    assert refusal


def test_legacy_human_status_keeps_its_existing_argument_shape():
    # Arrange
    delegation = backend.Delegation(
        ("systemd",), "status", True, "owned legacy Click command"
    )
    target = click.Command("status", params=[click.Argument(["name"])])
    # Act
    argv = backend.build_argv(
        delegation, name="owned-example", yes=False, leaf=target, exe="scitex-dev"
    )
    # Assert
    assert argv == ["scitex-dev", "ecosystem", "systemd", "status", "owned-example"]


@pytest.mark.parametrize("kind", ["service", "timer"])
def test_only_the_read_status_command_exposes_json(kind):
    # Arrange
    group = _make_group(kind)
    # Act
    json_commands = {
        name for name, command in group.commands.items()
        if any("--json" in parameter.opts for parameter in command.params)
    }
    # Assert
    assert json_commands == {"list", "status"}
