"""``sac accounts send-credentials``: a real peer list, and a live legacy alias.

Two of the three defects the operator found reading the shipped ``--help``:

1. The examples named peers that exist on no host (``compute-04``, ``laptop``),
   while ``--to`` requires a key from ``config.yaml``. Copying the
   documentation therefore failed.
2. ``keepalive`` needs a paragraph to say it copies credentials to peers - and
   the constitution's rule is that when a name needs restating as something
   else, that something else IS the name.

The alias tests are the load-bearing ones: ``keepalive`` is a PUBLISHED
contract with a scitex-dev JobSpec calling it every 15 minutes, so a rename
that stopped at renaming would take the fleet's credential distribution down.

(The ``--help`` SECTIONS are the third defect and live beside their own module,
in ``test_account_group_categories.py``.)
"""

from __future__ import annotations

import click
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._account_keepalive import (
    _registered_peer_lines,
    format_peer_lines,
    register_keepalive_command,
)


def _group() -> click.Group:
    """A bare group carrying only the commands under test."""
    group = click.Group("accounts")
    register_keepalive_command(group)
    return group


# --- the peer listing ------------------------------------------------------


def test_peer_lines_are_empty_when_no_peers_are_registered():
    # Arrange
    peers: list[str] = []

    # Act
    lines = format_peer_lines(peers)

    # Assert — a host with no peers gets no hint, never a bare header
    assert lines == []


def test_peer_lines_name_every_registered_peer():
    # Arrange
    peers = ["ywata-note-win", "scitex-compute-04"]

    # Act
    rendered = "\n".join(format_peer_lines(peers))

    # Assert
    assert "ywata-note-win" in rendered and "scitex-compute-04" in rendered


def test_peer_lines_mark_a_wildcard_row_as_a_pattern():
    # Arrange — `spartan-*` is a template for per-node keys, not a typable name
    peers = ["spartan-*"]

    # Act
    rendered = "\n".join(format_peer_lines(peers))

    # Assert
    assert "pattern" in rendered


def test_help_lists_the_peers_exactly_when_this_host_has_them():
    # Arrange — asserting `"Registered peers" in stdout` outright would be an
    # ENVIRONMENTAL fact, not a property of the code: the block renders from the
    # live config, so it is absent on any host with no peers registered (a CI
    # runner, a fresh checkout) and the test would fail there for being right.
    # The invariant that holds everywhere is that the two AGREE.
    group = _group()
    host_has_peers = bool(_registered_peer_lines())

    # Act
    result = CliRunner().invoke(group, ["send-credentials", "--help"])

    # Assert — wiring is present; what it renders is the host's business
    assert ("Registered peers" in result.stdout) == host_has_peers


# --- the examples ----------------------------------------------------------


def test_examples_do_not_name_the_peer_that_exists_nowhere():
    # Arrange
    group = _group()

    # Act
    result = CliRunner().invoke(group, ["send-credentials", "--help"])

    # Assert — `--to laptop` was in the shipped examples and is not a key
    assert "--to laptop" not in result.stdout


# --- the rename, which must not break the JobSpec that calls it ------------


def test_the_new_name_is_registered():
    # Arrange
    group = _group()

    # Act
    cmd = group.get_command(click.Context(group), "send-credentials")

    # Assert
    assert cmd is not None


def test_the_legacy_name_still_resolves():
    # Arrange — a JobSpec runs `accounts keepalive --all` every 15 minutes
    group = _group()

    # Act
    cmd = group.get_command(click.Context(group), "keepalive")

    # Assert
    assert cmd is not None


def test_the_legacy_name_still_accepts_the_same_options():
    # Arrange
    group = _group()
    ctx = click.Context(group)

    # Act
    legacy = group.get_command(ctx, "keepalive")
    current = group.get_command(ctx, "send-credentials")

    # Assert — same params, so the scheduled command line keeps parsing
    assert [p.name for p in legacy.params] == [p.name for p in current.params]


def test_the_legacy_name_is_hidden_from_help():
    # Arrange
    group = _group()

    # Act
    result = CliRunner().invoke(group, ["--help"])

    # Assert — deprecated, so it must not be advertised to new readers
    assert "keepalive" not in result.stdout


def test_the_legacy_name_warns_that_it_was_renamed():
    # Arrange — args that reach the callback, then fail on their own merits
    group = _group()
    args = ["keepalive", "--account", "no-such-account", "--to", "no-such-peer"]

    # Act
    result = CliRunner().invoke(group, args)

    # Assert
    assert "send-credentials" in result.stderr


def test_the_legacy_warning_never_lands_on_stdout():
    # Arrange — `--json` consumers parse stdout; a warning there breaks them
    group = _group()
    args = ["keepalive", "--account", "no-such-account", "--to", "no-such-peer"]

    # Act
    result = CliRunner().invoke(group, args)

    # Assert
    assert "renamed" not in result.stdout


def test_the_new_name_does_not_warn():
    # Arrange — the control: proves the warning is bound to the alias only
    group = _group()
    args = ["send-credentials", "--account", "no-such-account", "--to", "no-such"]

    # Act
    result = CliRunner().invoke(group, args)

    # Assert
    assert "is now" not in result.stderr
