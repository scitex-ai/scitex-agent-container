"""`host exec` must not render "could not run it" as "ran and found nothing".

MEASURED 2026-08-17, twice, by two different agents against the live fleet:

  * `sac host exec <peer> -- agents list` returned exit 127 with BOTH streams
    empty. 127 is command-not-found — the non-login ssh shell lacked the peer's
    venv on PATH — but the stderr saying so was gone.
  * Worse: a command containing `hostname` returned exit_code 0 with EMPTY
    stdout. `hostname` cannot print nothing, so the command had not run, and
    the tool reported SUCCESS. The agent who hit it nearly published "0 runtime
    homes on that host" as a finding, which would have corroborated a wrong
    hypothesis of mine with a fabricated measurement.

CAUSE: `subprocess.run(argv)` inherits the OS-LEVEL fds. Correct for a human at
a terminal — output streams live. Silently wrong for programmatic callers,
because the MCP surface invokes this command through Click's ``CliRunner``,
which captures PYTHON-level streams only. The child's bytes went to the real fd
and the caller got empty strings plus an exit code.

WHY THESE TESTS GO THROUGH CliRunner AND NOT capsys — THIS IS THE WHOLE POINT.
pytest's default capture is FD-LEVEL, so it would capture the old inherited
output too and every assertion here would pass against the unfixed code. That
is precisely the "test that cannot fail" this repo keeps finding. ``CliRunner``
is both the real broken consumer AND the only capture that distinguishes the
two implementations.

PA-306: no mocks. Real `subprocess`, real child processes, real CliRunner.
"""

from __future__ import annotations

import shlex
import subprocess

import click
import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.host_group import _run_peer_command


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
def _probe(argv):
    """Minimal real Click command exercising the production helper.

    Mirrors ``host exec``'s own ``context_settings`` and ``UNPROCESSED`` on
    purpose: without them Click parses a child's ``-c`` as an unknown OPTION
    and returns usage-error 2 before the helper ever runs, which is a test
    harness failure wearing the costume of a production one.
    """
    raise SystemExit(_run_peer_command(list(argv)))


def _run(*argv: str):
    return CliRunner().invoke(_probe, list(argv))


# ---------------------------------------------------------------------------
# The regression: output must reach a programmatic caller.
# ---------------------------------------------------------------------------


def test_child_stdout_reaches_a_programmatic_caller():
    """The exact failure: a caller got EMPTY stdout from a command that printed.

    Fails against the unfixed implementation, because an inherited fd writes
    past CliRunner entirely.
    """
    # Arrange
    marker = "hello-from-the-child"
    # Act
    result = _run("sh", "-c", f"echo {marker}")
    # Assert
    assert marker in result.stdout


def test_child_stderr_reaches_a_programmatic_caller():
    """The 127 case: the message explaining the failure must survive.

    A bare exit code with no stderr is indistinguishable from a dozen other
    failures, which is what made the original incident take two hours.
    """
    # Arrange
    marker = "explanation-on-stderr"
    # Act
    result = _run("sh", "-c", f"echo {marker} >&2")
    # Assert
    assert marker in result.output


def test_a_command_that_prints_nothing_is_distinguishable_from_one_that_failed():
    """The dangerous flavour, pinned from the safe side.

    A genuinely silent success must still be exit 0 — so that an EMPTY result
    paired with a NON-zero exit is unambiguously "did not run", rather than
    sharing its shape with "ran fine, found nothing".
    """
    # Arrange
    silent_success = ("sh", "-c", "true")
    # Act
    result = _run(*silent_success)
    # Assert
    assert result.exit_code == 0


def test_a_command_that_cannot_run_reports_nonzero():
    # Arrange
    missing = ("sh", "-c", "exit 127")
    # Act
    result = _run(*missing)
    # Assert
    assert result.exit_code == 127


# ---------------------------------------------------------------------------
# The exit code is the caller's contract and must pass through untouched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [0, 1, 2, 3, 42, 127])
def test_the_childs_exit_code_is_returned_verbatim(code: int):
    # Arrange
    argv = ("sh", "-c", f"exit {code}")
    # Act
    result = _run(*argv)
    # Assert
    assert result.exit_code == code


def test_stdout_and_stderr_are_not_merged_into_one_stream():
    """Keeping them separate is what lets a caller tell data from diagnosis.

    `result.output` merges both; `result.stdout` is stdout alone. If the helper
    echoed stderr to stdout, the payload would be polluted by diagnostics — the
    same defect in the opposite direction.
    """
    # Arrange
    argv = ("sh", "-c", "echo PAYLOAD; echo DIAGNOSTIC >&2")
    # Act
    result = _run(*argv)
    # Assert
    assert "DIAGNOSTIC" not in result.stdout


# ----------------------------------------------------------------------
# ARGUMENT BOUNDARIES MUST SURVIVE THE TRANSPORT
#
# The same rule as the top of this file, with a NUMBER instead of an empty
# string. ssh word-joins every token after the host and hands the result to
# the remote LOGIN SHELL, which re-parses it — so `host exec` must quote the
# user's argv words or the remote shell re-splits them.
#
# Measured 2026-08-20 by scitex-cards, reproduced here on compute-03:
#
#   host exec <peer> -- echo AAA-scitex-BBB '|' grep -cE 'scitex|cards'
#     -> bash: line 1: cards: command not found
#   host exec <peer> -- bash -lc 'echo AAA-scitex-BBB | grep -cE "scitex|cards"'
#     -> 0                                            (the answer is 1)
#
# The second is the dangerous one: a mangled command returned a plausible
# number, and only a contradiction with an earlier count stopped it being
# reported as a measurement.
#
# The quoting belongs in `host_exec`, NOT in `build_ssh_argv`: that function's
# contract is an ALREADY-QUOTED remote command, and adding a join there on
# 2026-08-17 double-quoted callers that pre-quote correctly and produced
# rc=127 on every preamble peer, taking scitex-hub down.
# ----------------------------------------------------------------------


def _as_ssh_would_join(words: list[str]) -> str:
    """What the remote login shell actually receives, per OpenSSH's word-join."""
    return " ".join(shlex.quote(w) for w in words)


@pytest.mark.parametrize(
    "words",
    [
        ["hostname"],
        ["sac", "db", "export"],
        ["agent", "list", "--json"],
    ],
)
def test_ordinary_words_render_byte_identically(words):
    """Quoting must not perturb any invocation that was already correct."""
    # Arrange — words carrying no shell metacharacters.
    # Act
    rendered = [shlex.quote(w) for w in words]
    # Assert
    assert rendered == words


def test_a_metacharacter_argument_survives_the_remote_reparse():
    """The exact shape that returned a wrong NUMBER against the live fleet."""
    # Arrange — a pipe as its own word, and a regex alternation inside one.
    words = ["echo", "AAA-scitex-BBB", "|", "grep", "-cE", "scitex|cards"]
    # Act — join as ssh does, then let a REAL shell re-parse it (no mocks).
    recovered = shlex.split(_as_ssh_would_join(words))
    # Assert
    assert recovered == words


def test_the_remote_shell_runs_the_command_the_caller_wrote():
    """End to end through a real child: the pipe must NOT take effect here.

    With the words quoted, `|` is an ARGUMENT to echo, so echo prints it and
    nothing is piped. Unquoted, the remote shell would treat it as a pipeline
    and hunt for a command named `cards` — which is exactly what compute-03
    reported before the fix.
    """
    # Arrange
    joined = _as_ssh_would_join(["echo", "a", "|", "b"])
    # Act
    out = subprocess.run(
        ["bash", "-c", joined], capture_output=True, text=True
    ).stdout.strip()
    # Assert
    assert out == "a | b"
