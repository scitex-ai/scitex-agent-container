"""The entry-point gate must observe the UNION, and must be able to say NO.

Pins the 2026-08-03 incident: scitex-hub's a2a inbox rail was dead for sixteen
days because three console-script wrappers were masked by overlay whiteouts.
The SIF shipped them and `import scitex_agent_container` succeeded throughout,
so every gate that existed was green about a filesystem that was not broken.

Two properties carry the weight here, and neither is "it returns None":

1. THE PROBE MEASURES THE SAME UNION THE AGENT LAUNCHES IN. A probe that
   rebuilt the apptainer preamble itself could drift from the real launch and
   report on an overlay nobody runs in. So the tests assert the probe inherits
   the launch argv's `--overlay` value, not merely that a probe was produced.

2. THE GATE CAN FIRE. "violation is None" passes when the gate works, when it
   never ran, and when it cannot fail -- three causes, one observation. So a
   non-zero runner must produce an accusation naming the repair.

No mocks: `runner` is a plain injected callable, the same seam used by
`Registry.cleanup_stale(probe=...)`.

PA-307 / STX-TQ002 / STX-TQ007 -- one assert per test, full AAA markers.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._entry_point_gate import (
    DEFAULT_CONSOLE_SCRIPT,
    EntryPointGateError,
    assert_entry_point_runs,
    entry_point_violation,
    probe_argv_from_launch,
)

# A realistic launch argv: isolation flags, the overlay that actually broke
# hub, the image, then the inner command the probe must REPLACE.
_OVERLAY = "/home/ywatanabe/.scitex/agent-container/containers/overlays/scitex-hub/"
_SIF = "/home/ywatanabe/.scitex/.../sac-base-2026-0802-225220.sif"
_LAUNCH = [
    "apptainer",
    "exec",
    "--containall",
    "--overlay",
    _OVERLAY,
    _SIF,
    "claude",
    "--dangerously-skip-permissions",
]


def _runner_returning(code: int):
    """A probe runner that reports ``code`` and records what it was asked."""

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> int:
        calls.append(list(argv))
        return code

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_probe_inherits_the_launch_overlay():
    # Arrange — the whiteout lives in the OVERLAY; a probe that dropped this
    # flag would exercise the pristine image and pass while the agent is broken.
    launch = list(_LAUNCH)
    # Act
    probe = probe_argv_from_launch(launch)
    # Assert
    assert _OVERLAY in probe


def test_probe_keeps_the_image_it_was_given():
    # Arrange
    launch = list(_LAUNCH)
    # Act
    probe = probe_argv_from_launch(launch)
    # Assert
    assert _SIF in probe


def test_probe_replaces_the_inner_command_with_the_console_script():
    # Arrange — the agent's own command must NOT run; this is a probe.
    launch = list(_LAUNCH)
    # Act
    probe = probe_argv_from_launch(launch)
    # Assert
    assert probe[-2:] == [DEFAULT_CONSOLE_SCRIPT, "--version"]


def test_probe_does_not_carry_the_agents_own_command():
    # Arrange
    launch = list(_LAUNCH)
    # Act
    probe = probe_argv_from_launch(launch)
    # Assert
    assert "claude" not in probe


def test_an_argv_with_no_image_is_unprobeable():
    # Arrange — nothing to mount means nothing to measure.
    launch = ["apptainer", "exec", "--containall"]
    # Act
    probe = probe_argv_from_launch(launch)
    # Assert
    assert probe == []


def test_a_working_console_script_is_not_a_violation():
    # Arrange
    runner = _runner_returning(0)
    # Act
    violation = entry_point_violation(_LAUNCH, runner=runner)
    # Assert
    assert violation is None


def test_the_gate_actually_runs_the_probe():
    # Arrange — "no violation" must come from EXERCISING the union, not from
    # inspecting the argv's shape. If the runner is never called, the gate is
    # deciding on paper.
    runner = _runner_returning(0)
    # Act
    entry_point_violation(_LAUNCH, runner=runner)
    # Assert
    assert runner.calls == [[*_LAUNCH[:6], DEFAULT_CONSOLE_SCRIPT, "--version"]]


def test_a_missing_console_script_is_a_violation():
    # Arrange — THE MUTATION PROOF. Exit 127 is what a masked wrapper gives.
    runner = _runner_returning(127)
    # Act
    violation = entry_point_violation(_LAUNCH, runner=runner)
    # Assert
    assert violation is not None


def test_the_violation_names_the_whiteout_repair():
    # Arrange — a gate that fires without naming the fix costs the next reader
    # the same sixteen days of diagnosis.
    runner = _runner_returning(127)
    # Act
    violation = entry_point_violation(_LAUNCH, runner=runner)
    # Assert
    assert "whiteout" in violation


def test_an_unprobeable_launch_never_accuses():
    # Arrange — no image: the probe cannot run. "I could not look" must not be
    # reported as "it is broken"; the same tri-state rule that stopped the
    # registry sweep from deleting live agents.
    runner = _runner_returning(127)
    # Act
    violation = entry_point_violation(["apptainer", "exec"], runner=runner)
    # Assert
    assert violation is None


# ---------------------------------------------------------------------------
# The wired gate. An unwired checker is a remedy written, not exercised — the
# start path must actually REFUSE, because a log line would reproduce the exact
# failure being prevented (an agent that looks healthy and answers nobody).
# ---------------------------------------------------------------------------


def test_the_gate_refuses_a_start_when_the_console_script_is_missing():
    # Arrange
    runner = _runner_returning(127)
    raised = None
    # Act
    try:
        assert_entry_point_runs("scitex-hub", _LAUNCH, runner=runner)
    except EntryPointGateError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, EntryPointGateError)


def test_the_gate_allows_a_start_when_the_console_script_runs():
    # Arrange — the gate must not be a wall; a working agent has to start.
    runner = _runner_returning(0)
    # Act
    result = assert_entry_point_runs("scitex-hub", _LAUNCH, runner=runner)
    # Assert
    assert result is None


def test_an_apptainer_level_failure_is_not_blamed_on_the_console_script():
    # Arrange — EXIT-CODE COLLISION, caught the hard way. `apptainer exec`
    # returns the inner command's status on success, but its OWN failures
    # (missing image, mount error) come back non-zero too — 255, typically.
    # The first version of this gate read any non-zero as "wrapper missing" and
    # refused 29 legitimate starts in the tui_session suite, which injects a
    # fake argv naming an image that does not exist. 255 is UNKNOWN, not guilt.
    runner = _runner_returning(255)
    # Act
    violation = entry_point_violation(_LAUNCH, runner=runner)
    # Assert
    assert violation is None


def test_a_not_executable_wrapper_is_still_a_violation():
    # Arrange — 126 is the other half of the not-found pair; narrowing to 127
    # alone would let a present-but-unrunnable wrapper through.
    runner = _runner_returning(126)
    # Act
    violation = entry_point_violation(_LAUNCH, runner=runner)
    # Assert
    assert violation is not None


def test_a_probe_that_raises_never_blocks_a_start():
    # Arrange — THE ANTI-BRICK CASE. A probe bug (missing apptainer, timeout)
    # must not refuse every start on the host; that would be a gate more
    # dangerous than the fault it guards.
    def exploding_runner(argv):
        raise OSError("apptainer not found")

    # Act
    result = assert_entry_point_runs("scitex-hub", _LAUNCH, runner=exploding_runner)
    # Assert
    assert result is None
