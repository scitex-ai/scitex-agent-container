"""Sandbox creation separates argument previews from explicitly approved writes."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import image_group as ig
from scitex_agent_container.cli_pkg._main import main


@pytest.fixture
def sandbox_backend(tmp_path):
    source = tmp_path / "input file.sif"
    source.write_bytes(b"owned non-executable fixture")
    output = tmp_path / "output sandbox"
    calls = []

    class Backend:
        def sandbox_create(self, **kwargs):
            calls.append(kwargs)
            dest = kwargs["output_dir"]
            dest.mkdir()
            (dest / "fixture.txt").write_text("created by inert backend")
            return dest

    def load():
        calls.append("backend-loaded")
        return Backend()

    previous = ig._load_apptainer
    ig._load_apptainer = load
    try:
        yield source, output, calls
    finally:
        ig._load_apptainer = previous


@pytest.mark.parametrize("command", ["create-sandbox", "sandbox"])
def test_creation_requires_approval_before_loading_backend(sandbox_backend, command):
    # Arrange
    source, output, calls = sandbox_backend
    # Act
    result = CliRunner().invoke(
        main, ["image", command, str(source), "--output", str(output)]
    )
    # Assert
    assert (result.exit_code, "--yes" in result.output, calls, output.exists()) == (
        2,
        True,
        [],
        False,
    )


@pytest.mark.parametrize("command", ["create-sandbox", "sandbox"])
@pytest.mark.parametrize(
    "flags", [["--dry-run"], ["--yes", "--dry-run"], ["--dry-run", "-y"]]
)
def test_preview_does_not_resolve_source_or_create_output(
    sandbox_backend, command, flags
):
    # Arrange
    _, output, calls = sandbox_backend
    # Act
    result = CliRunner().invoke(
        main,
        ["--json", "image", command, "missing-source", "--output", str(output), *flags],
    )
    # Assert
    assert (result.exit_code, calls, output.exists()) == (0, [], False)


def test_preview_reports_arguments_without_claiming_resolved_image(sandbox_backend):
    # Arrange
    _, output, _ = sandbox_backend
    # Act
    result = CliRunner().invoke(
        main,
        [
            "image",
            "create-sandbox",
            "missing-source",
            "--output",
            str(output),
            "--dry-run",
        ],
    )
    # Assert
    assert json.loads(result.stdout) == {
        "action": "create-sandbox",
        "source": "missing-source",
        "output": str(output),
        "dry_run": True,
    }


@pytest.mark.parametrize("command", ["create-sandbox", "sandbox"])
@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_approved_creation_keeps_backend_arguments(sandbox_backend, command, approval):
    # Arrange
    source, output, calls = sandbox_backend
    # Act
    result = CliRunner().invoke(
        main, ["image", command, str(source), "--output", str(output), approval]
    )
    # Assert
    assert (result.exit_code, calls) == (
        0,
        [
            "backend-loaded",
            {
                "source": source.resolve(),
                "containers_dir": ig._CONTAINERS_DIR,
                "output_dir": output,
            },
        ],
    )


def test_legacy_alias_warns_only_on_stderr(sandbox_backend):
    # Arrange
    _, output, _ = sandbox_backend
    # Act
    result = CliRunner().invoke(
        main,
        ["image", "sandbox", "missing-source", "--output", str(output), "--dry-run"],
    )
    # Assert
    assert (json.loads(result.stdout)["action"], "create-sandbox" in result.stderr) == (
        "create-sandbox",
        True,
    )


def test_image_help_lists_only_canonical_creation_command():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(main, ["image", "--help"])
    # Assert
    assert (
        "\n  create-sandbox " in result.stdout,
        "\n  sandbox " in result.stdout,
    ) == (True, False)
