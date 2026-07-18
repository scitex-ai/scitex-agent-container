"""Tests for ``sac image bake-remote`` (the CLI face of the remote bake).

No mocks: the subprocess seam (``_remote_bake_core._run``) is swapped for
a hand-rolled fake that returns real ``CompletedProcess`` objects with
scripted remote output (save/restore pattern, as in ``test_image_group``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import _image_remote_bake as mod
from scitex_agent_container.cli_pkg import _remote_bake_core as core
from scitex_agent_container.cli_pkg._image_remote_bake import image_bake_remote
from scitex_agent_container.cli_pkg._remote_bake_core import BakeVerdict
from scitex_agent_container.cli_pkg.image_group import image_group


class _SshRunner:
    """Fake for the ssh leg: scripted stdout/rc, argv + stdin recorded."""

    def __init__(self, *, stdout: str, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.saw_stdin: list[bool] = []
        self._stdout = stdout
        self._rc = returncode

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.saw_stdin.append(kwargs.get("stdin") is not None)
        return subprocess.CompletedProcess(args, self._rc, self._stdout, "")


@pytest.fixture()
def seam():
    saved = core._run

    def _swap(runner):
        core._run = runner
        return runner

    yield _swap
    core._run = saved


_BAKED = (
    'SAC_BAKE_RESULT={"verdict":"BAKED","layer":"base","ts":"2026-0717-000000",'
    '"head":"abc","sif":"/store/sac-base/sac-base-2026-0717-000000.sif",'
    '"sha256":"deadbeef","pruned":"","duration_sec":10}\n'
)


def test_bake_remote_is_registered_on_the_image_group() -> None:
    # Arrange
    group = image_group
    # Act
    registered = "bake-remote" in group.commands
    # Assert
    assert registered


def test_refuses_to_run_without_yes(seam) -> None:
    # Arrange — mirror `sac image build`: non-interactive runs must be
    # explicit or the verb refuses (exit 2), so a timer command missing
    # --yes fails loudly on night one, not silently forever.
    runner = seam(_SshRunner(stdout=_BAKED))
    # Act
    result = CliRunner().invoke(image_bake_remote, [])
    # Assert
    assert (result.exit_code, runner.calls) == (2, [])


def test_bake_only_pipes_the_wheel_script_over_ssh(seam) -> None:
    # Arrange
    runner = seam(_SshRunner(stdout=_BAKED))
    # Act
    CliRunner().invoke(image_bake_remote, ["--layer", "base", "--bake-only", "--yes"])
    # Assert — the script travels on stdin; nothing is deployed remotely.
    assert runner.saw_stdin == [True]


def test_bake_only_targets_the_configured_host(seam) -> None:
    # Arrange
    runner = seam(_SshRunner(stdout=_BAKED))
    # Act
    CliRunner().invoke(
        image_bake_remote,
        ["--layer", "base", "--bake-only", "--yes", "--host", "spartan-alt"],
    )
    # Assert — the host is the second-to-last argv element (the last is
    # the remote command string), robust to ssh option insertions.
    assert runner.calls[0][-2] == "spartan-alt"


def test_bake_only_success_exits_zero(seam) -> None:
    # Arrange
    seam(_SshRunner(stdout=_BAKED))
    # Act
    result = CliRunner().invoke(
        image_bake_remote, ["--layer", "base", "--bake-only", "--yes"]
    )
    # Assert
    assert result.exit_code == 0


def test_remote_failure_exits_nonzero(seam) -> None:
    # Arrange
    seam(
        _SshRunner(
            stdout=(
                'SAC_BAKE_RESULT={"verdict":"FAILED","layer":"base",'
                '"step":"quota","reason":"quota-low"}\n'
            ),
            returncode=1,
        )
    )
    # Act
    result = CliRunner().invoke(
        image_bake_remote, ["--layer", "base", "--bake-only", "--yes"]
    )
    # Assert
    assert result.exit_code == 1


def test_dead_remote_with_no_verdict_exits_nonzero(seam) -> None:
    # Arrange — no SAC_BAKE_RESULT line at all (ssh died): NO_RESULT is a
    # failure state, never an ok.
    seam(_SshRunner(stdout="Connection closed by remote host\n", returncode=255))
    # Act
    result = CliRunner().invoke(
        image_bake_remote, ["--layer", "base", "--bake-only", "--yes"]
    )
    # Assert
    assert result.exit_code == 1


def test_green_verdict_on_red_ssh_exit_is_refused(seam) -> None:
    # Arrange — a BAKED line but ssh rc=1: the contradiction must be
    # refused (NO_RESULT), not resolved in favour of the green.
    runner = seam(_SshRunner(stdout=_BAKED, returncode=1))
    # Act
    outcome = mod.run_remote_bake(
        host="spartan",
        layer="base",
        lease_name="lease",
        remote_workdir="/w",
        branch="develop",
        retain=3,
        force=False,
        timeout=60,
    )
    # Assert
    assert (outcome.verdict, len(runner.calls)) == (BakeVerdict.NO_RESULT, 1)


def test_missing_wheel_script_is_a_loud_click_error() -> None:
    # Arrange — a wheel that lost the script must not ssh at all.
    missing = Path("/nonexistent/spartan-sif-bake.sh")

    # Act
    def _call():
        mod.run_remote_bake(
            host="spartan",
            layer="base",
            lease_name="lease",
            remote_workdir="/w",
            branch="develop",
            retain=3,
            force=False,
            timeout=60,
            script_path=missing,
        )

    # Assert
    import click

    with pytest.raises(click.ClickException):
        _call()
