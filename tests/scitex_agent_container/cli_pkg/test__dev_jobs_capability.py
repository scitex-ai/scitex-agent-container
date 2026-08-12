"""The job-name argument shape is READ from the installed CLI, not assumed.

WHAT THIS GUARDS, AND WHAT IT COST TO LEARN

``build_argv`` emitted ``--name X`` unconditionally. That was correct for
every scitex-dev up to 0.47.0. **0.48.0 made the shape mixed:**

    install / uninstall                                       --name X
    status / enable / disable / start / stop / restart / exec  NAME

Sending the old shape to a new positional verb is rejected by Click
BEFORE the command runs — ``Error: No such option '--name'``, exit 2.
Exit 2 is indistinguishable, from the outside, from sac declining the
verb on capability grounds; the suite asserted exit 4 (sac's refusal
code), saw 2, and read it as a refusal regression. The real defect was
that sac never managed to invoke the command at all.

That misreading is why these tests exist and why the shape is
introspected rather than tabulated: a hard-coded verb->shape table is a
copy of somebody else's CLI, and this incident is what a stale copy
costs — a night of fleet-wide red, hunted through runners, Python
versions and branch staleness before the dependency was suspected.

NO MOCKS, AND NO ``monkeypatch``. Both shapes below are REAL ``click``
commands, declared exactly as the two scitex-dev generations declare
them: a real ``--name`` option and a real positional argument. They are
not stand-ins for the input under test — they ARE the two inputs the
predicate must tell apart. ``build_argv`` takes the resolved leaf command
as a PARAMETER, so both worlds are exercised by passing a real command
rather than by rewriting the module's internals. The "cannot read the
tree" case uses a path that genuinely does not exist in any scitex-dev,
so even that answer comes from the real probe.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import click
import pytest

from scitex_agent_container.cli_pkg import _dev_jobs_backend as backend
from scitex_agent_container.cli_pkg import _dev_jobs_capability as capability


@click.command("install")
@click.option("--name", required=True)
def option_shaped(name: str) -> None:  # pragma: no cover - never invoked
    """The pre-0.48.0 shape, and still 0.48.0's shape for install/uninstall."""


@click.command("status")
@click.argument("name")
def positional_shaped(name: str) -> None:  # pragma: no cover - never invoked
    """0.48.0's shape for status/enable/disable/start/stop/restart/exec."""


@click.command("list")
def nameless() -> None:  # pragma: no cover - never invoked
    """A verb that takes no job name at all."""


#: A path no scitex-dev has ever mounted, so the real probe genuinely
#: cannot resolve it. Used instead of patching the lookup away.
UNRESOLVABLE_PATH = ("no-such-group-6f2a",)


def _delegation(verb: str, path: tuple[str, ...] = ("dev", "timer")) -> backend.Delegation:
    return backend.Delegation(
        path=path, verb=verb, supported=True, evidence="constructed by a test"
    )


# ---------------------------------------------------------------------------
# The predicate, against real commands of both shapes.
# ---------------------------------------------------------------------------


def test_an_option_shaped_command_is_read_as_an_option() -> None:
    # Arrange
    command = option_shaped
    # Act
    is_option = capability.name_is_an_option(command)
    # Assert
    assert is_option is True, (
        "a command declaring `--name` was not read as taking an option, so "
        "sac would pass the job name positionally to a verb that wants a flag."
    )


def test_a_positional_shaped_command_is_not_read_as_an_option() -> None:
    # Arrange
    command = positional_shaped
    # Act
    is_option = capability.name_is_an_option(command)
    # Assert
    assert is_option is False, (
        "a command declaring a positional NAME was read as taking `--name`. "
        "That is the 0.48.0 defect exactly: Click rejects the option before "
        "the command runs, and the exit code then looks like a capability "
        "refusal rather than a failed invocation."
    )


def test_a_command_with_no_name_at_all_is_not_read_as_an_option() -> None:
    # Arrange
    command = nameless
    # Act
    is_option = capability.name_is_an_option(command)
    # Assert
    assert is_option is False


def test_an_unresolvable_path_is_unknown_rather_than_a_guess() -> None:
    # Arrange — the third state, from the REAL probe: a group no
    # scitex-dev mounts. "I could not read the CLI" must not be reported
    # as either shape.
    path = UNRESOLVABLE_PATH
    # Act
    style = capability.name_style_for(path, "status")
    # Assert
    assert style == "unknown", (
        f"probing {path} answered {style!r}. A probe that cannot read the "
        "tree must say so; guessing is what turns a dependency bump into a "
        "silent argv error."
    )


def test_the_installed_scitex_dev_answers_one_of_the_three_states() -> None:
    # Arrange — whatever is installed here, the contract is total.
    delegation = backend.resolve("timer", "install")
    # Act
    style = capability.name_style_for(delegation.path, delegation.verb)
    # Assert
    assert style in {"option", "positional", "unknown"}, (
        f"name_style_for returned {style!r}, which is not one of the three "
        "declared states; build_argv branches on exactly those."
    )


# ---------------------------------------------------------------------------
# The argv that actually ships, under both worlds.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "leaf, expected_tail",
    [
        (option_shaped, ["--name", "sac.accounts-refresh"]),
        (positional_shaped, ["sac.accounts-refresh"]),
    ],
    ids=["scitex-dev<=0.47-option", "scitex-dev>=0.48-positional"],
)
def test_build_argv_follows_the_installed_commands_shape(leaf, expected_tail) -> None:
    # Arrange — a REAL command of the shape under test, injected, so both
    # scitex-dev generations are exercised from whichever single version
    # happens to be installed on this machine.
    delegation = _delegation("status")
    # Act
    argv = backend.build_argv(
        delegation, name="sac.accounts-refresh", yes=False, leaf=leaf
    )
    # Assert
    assert argv[-len(expected_tail) :] == expected_tail, (
        f"build_argv produced {argv!r}, which does not end in "
        f"{expected_tail!r}. The job name must be passed the way the "
        "installed command declares it, or Click refuses the invocation "
        "before the verb ever runs."
    )


def test_an_unknown_shape_keeps_the_pre_048_option_form() -> None:
    # Arrange — an unresolvable path, so the real probe returns "unknown".
    # Every reachable older scitex-dev wants `--name`, and both wrong
    # guesses fail the same safe way (a Click usage error, before any host
    # state changes), so the older form is the deliberate default.
    delegation = _delegation("status", path=UNRESOLVABLE_PATH)
    # Act
    argv = backend.build_argv(delegation, name="sac.accounts-refresh", yes=False)
    # Assert
    assert argv[-2:] == ["--name", "sac.accounts-refresh"]


def test_a_delegation_without_a_name_gains_no_name_argument() -> None:
    # Arrange — `list` takes no job name; the shape probe must not invent one.
    delegation = _delegation("list")
    # Act
    argv = backend.build_argv(delegation, name=None, yes=False, leaf=nameless)
    # Assert
    assert argv == ["scitex-dev", "ecosystem", "dev", "timer", "list"]


def test_the_forwarded_flags_survive_the_positional_shape() -> None:
    # Arrange — the --dry-run/--yes pass-through is load-bearing: it keeps
    # `timer disable sac.accounts-refresh` guarded, on the fleet's sole
    # OAuth refresher. Changing the name shape must not disturb it.
    delegation = _delegation("disable")
    # Act
    argv = backend.build_argv(
        delegation,
        name="sac.accounts-refresh",
        yes=True,
        dry_run=True,
        leaf=positional_shaped,
    )
    # Assert
    assert argv[-3:] == ["sac.accounts-refresh", "--dry-run", "--yes"]
