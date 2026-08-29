"""A laptop being off must not make the credential alarm red.

WHY THIS EXISTS (2026-08-16):

`scitex-agent-container-accounts-keepalive` is the DISTRIBUTION half of the
single-refresher model — the only thing keeping access-only hosts alive. It
had been sitting in `failed` state, and the measured reason was one line:

    ywata-note-win  ...: FAILED — ssh: connect to host 192.168.11.101
                                  port 22: No route to host

Every other peer in that same run reported `already current ... (verified
HTTP 200)`. The credentials were entirely healthy; the operator's LAPTOP was
closed.

The unit exits non-zero if ANY peer fails, so a portable machine being off
pins the fleet's credential alarm red permanently. That is worse than having
no alarm: a signal that is always red is one nobody reads, and this is the
signal that says agents are about to start 401ing.

THE FIX IS A DECLARATION, NOT A DETECTOR. It would be easy to sniff the error
text for "No route to host" and treat network failures as soft. That is
exactly the implicit surprise logic the operator has ruled against — you
could not tell from the unit file which hosts may be absent. Instead the
caller NAMES the intermittent peers, so `systemctl cat` shows you:

    ... --to ywata-note-win --optional-peer ywata-note-win

and the tolerance is visible at the call site.

Tolerated failures are still pushed, still printed, still recorded with
`"optional": true` in --json, and still summarised on the TOLERATED line. The
run forgives them; it never hides them.
"""

import pytest
from click.testing import CliRunner


@pytest.fixture
def keepalive_cli():
    """The account group with the keepalive command registered."""
    import click

    from scitex_agent_container.cli_pkg._account_keepalive import (
        register_keepalive_command,
    )

    @click.group()
    def group():
        pass

    register_keepalive_command(group)
    return group


@pytest.fixture
def undeclared_target_result(keepalive_cli):
    """Invoke with an --optional-peer that is NOT among the --to targets."""
    return CliRunner().invoke(
        keepalive_cli,
        [
            "keepalive",
            "--account", "alpha-example-com",
            "--to", "compute-04",
            "--optional-peer", "a-host-not-in-to",
        ],
    )


@pytest.fixture
def declared_target_result(keepalive_cli):
    """Invoke with an --optional-peer that IS among the --to targets."""
    return CliRunner().invoke(
        keepalive_cli,
        [
            "keepalive",
            "--account", "alpha-example-com",
            "--to", "ywata-note-win",
            "--optional-peer", "ywata-note-win",
        ],
    )


@pytest.fixture
def help_result(keepalive_cli):
    """`keepalive --help`, which is what a reader of the unit file consults."""
    return CliRunner().invoke(keepalive_cli, ["keepalive", "--help"])


def test_optional_peer_absent_from_to_is_refused(undeclared_target_result):
    """A declaration that applies to nothing is a typo, not a tolerance."""
    # Arrange: see fixture
    result = undeclared_target_result

    # Act
    exit_code = result.exit_code

    # Assert
    assert exit_code != 0


def test_the_refusal_names_the_offending_host(undeclared_target_result):
    """Naming it is what makes the typo fixable without rerunning."""
    # Arrange: see fixture
    result = undeclared_target_result

    # Act
    output = result.output

    # Assert
    assert "a-host-not-in-to" in output


def test_a_declared_optional_peer_passes_the_guard(declared_target_result):
    """The mirror case.

    Without this, a guard that rejected EVERY optional peer would satisfy the
    refusal tests above while making the flag unusable — green by refusing,
    which is the cannot-succeed twin of a gate that cannot fail.
    """
    # Arrange: see fixture
    result = declared_target_result

    # Act: the guard's own message is the discriminator. The command may still
    # fail later for environmental reasons (no such stored account on this
    # machine), so exit 0 is the wrong thing to assert.
    output = result.output

    # Assert
    assert "absent from --to" not in output


def test_help_advertises_the_flag(help_result):
    """The unit file is the record, so the flag must be discoverable."""
    # Arrange: see fixture
    result = help_result

    # Act
    output = result.output

    # Assert
    assert "--optional-peer" in output


def test_help_explains_what_optional_means(help_result):
    """A flag named without its meaning invites use on an always-on host."""
    # Arrange: see fixture
    result = help_result

    # Act
    output = result.output

    # Assert
    assert "INTERMITTENT" in output
