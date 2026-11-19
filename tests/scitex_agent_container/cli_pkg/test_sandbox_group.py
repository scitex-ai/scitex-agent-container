"""``sac sandbox exec`` CLI parsing (``--dry-run``: no apptainer, no disk writes)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.sandbox_group import sandbox_group

RUN_ROOT = "/srv/sac-exec-cli-unit-runs"  # validated only; never created


def _invoke(tmp_path, *extra, command=("python3", "-c", "print(1+1)")):
    image = tmp_path / "img.sif"
    image.write_bytes(b"sif")
    args = ["exec", "--image", str(image), "--run-root", RUN_ROOT, *extra]
    return CliRunner().invoke(sandbox_group, [*args, "--dry-run", "--", *command])


def test_dry_run_prints_the_command_record(tmp_path):
    # Arrange
    extra = ("--timeout", "30")
    # Act
    result = _invoke(tmp_path, *extra)
    # Assert
    assert json.loads(result.output)["command"]["argv"] == [
        "python3",
        "-c",
        "print(1+1)",
    ]


def test_flags_after_the_separator_belong_to_the_command(tmp_path):
    # Arrange
    command = ("pip", "download", "--no-deps", "--timeout", "5", "six")
    # Act
    result = _invoke(tmp_path, "--timeout", "30", command=command)
    # Assert
    assert json.loads(result.output)["argv"][-6:] == list(command)


def test_repeated_egress_flags_are_all_allowed(tmp_path):
    # Arrange
    extra = ("--timeout", "30", "--egress-allow", "pypi.org:443",
             "--egress-allow", "files.pythonhosted.org:443")  # fmt: skip
    # Act
    result = _invoke(tmp_path, *extra)
    # Assert
    assert json.loads(result.output)["policy"]["egress_allow"] == [
        "pypi.org:443",
        "files.pythonhosted.org:443",
    ]


def test_bind_flag_is_read_only_by_default(tmp_path):
    # Arrange
    extra = ("--timeout", "30", "--bind", f"{tmp_path}:/bundle")
    # Act
    result = _invoke(tmp_path, *extra)
    # Assert
    assert json.loads(result.output)["command"]["binds"] == [
        f"{tmp_path.resolve()}:/bundle:ro"
    ]


def test_missing_timeout_is_a_usage_error(tmp_path):
    # Arrange
    extra: tuple[str, ...] = ()
    # Act
    result = _invoke(tmp_path, *extra)
    # Assert
    assert result.exit_code == 2


def test_tmp_run_root_is_a_usage_error(tmp_path):
    # Arrange
    image = tmp_path / "img.sif"
    image.write_bytes(b"sif")
    args = ["exec", "--image", str(image), "--run-root", "/tmp/x", "--timeout", "9",
            "--dry-run", "--", "true"]  # fmt: skip
    # Act
    result = CliRunner().invoke(sandbox_group, args)
    # Assert
    assert result.exit_code == 2 and "never tmp" in result.output
