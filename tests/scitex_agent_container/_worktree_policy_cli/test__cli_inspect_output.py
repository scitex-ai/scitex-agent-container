"""Inspection output choices preserve native decisions and error protocols."""

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main

from .test__cli import _authority, _run


def _inspect(tmp_path, route, *options):
    args = ["inspect", "--repo", str(tmp_path), *options]
    if route == "native":
        result = _run(tmp_path, *args)
        return result.returncode, result.stdout, result.stderr
    result = CliRunner(
        env={
            "SCITEX_AGENT_CONTAINER_HOME": str(tmp_path / "sac-state"),
            "SCITEX_DIR": str(tmp_path / "scitex"),
        }
    ).invoke(main, ["worktree", "policy", *args])
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.parametrize("route", ["native", "click"])
def test_explicit_json_preserves_default_inspection(tmp_path, route):
    # Arrange
    _authority(tmp_path)

    # Act
    default = _inspect(tmp_path, route)
    explicit = _inspect(tmp_path, route, "--json")

    # Assert
    assert (
        default[0], explicit[0], json.loads(default[1]),
        json.loads(explicit[1])["decision"],
    ) == (0, 0, json.loads(explicit[1]), "allow")


@pytest.mark.parametrize("route", ["native", "click"])
def test_text_view_contains_every_inspection_field(tmp_path, route):
    # Arrange
    _authority(tmp_path)
    expected = json.loads(_inspect(tmp_path, route, "--json")[1])

    # Act
    code, stdout, _ = _inspect(tmp_path, route, "--text")
    fields = dict(line.split(": ", 1) for line in stdout.splitlines())

    # Assert
    assert (code, {key: json.loads(value) for key, value in fields.items()}) == (
        0, expected,
    )


@pytest.mark.parametrize("route", ["native", "click"])
@pytest.mark.parametrize("flags", [("--json", "--text"), ("--text", "--json")])
def test_conflicting_output_choices_keep_json_argument_errors(tmp_path, route, flags):
    # Arrange
    _authority(tmp_path)

    # Act
    code, stdout, stderr = _inspect(tmp_path, route, *flags)
    payload = json.loads(stdout)

    # Assert
    assert (
        code, payload["allowed"], payload["decision"],
        "not allowed with argument" in payload["error"],
        "--json" in stderr and "--text" in stderr,
    ) == (3, False, "error", True, True)


@pytest.mark.parametrize("route", ["native", "click"])
def test_text_request_does_not_change_policy_error_protocol(tmp_path, route):
    # Arrange
    _authority(tmp_path)
    policy = tmp_path / "sac-state" / "worktree-policy" / "worktree-policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text("{}")

    # Act
    code, stdout, _ = _inspect(tmp_path, route, "--text")
    payload = json.loads(stdout)

    # Assert
    assert (
        code, payload["allowed"], payload["decision"],
        "policy missing keys" in payload["error"],
    ) == (3, False, "error", True)


def test_discovery_exposes_native_inspection_output_flags():
    # Arrange
    operation = main.commands["worktree"].commands["policy"].commands["inspect"]

    # Act
    options = {p.name: p for p in operation.params}

    # Assert
    assert (
        set(options), options["repo"].required,
        options["json"].is_flag, options["text"].is_flag,
        options["json"].default, options["text"].default,
    ) == ({"repo", "json", "text"}, True, True, True, False, False)


def test_global_json_takes_precedence_over_inspection_text(tmp_path):
    # Arrange
    _authority(tmp_path)
    runner = CliRunner(
        env={
            "SCITEX_AGENT_CONTAINER_HOME": str(tmp_path / "sac-state"),
            "SCITEX_DIR": str(tmp_path / "scitex"),
        }
    )
    expected = json.loads(_inspect(tmp_path, "click", "--json")[1])

    # Act
    result = runner.invoke(
        main,
        ["--json", "worktree", "policy", "inspect", "--repo", str(tmp_path), "--text"],
    )

    # Assert
    assert (result.exit_code, json.loads(result.stdout)) == (0, expected)
