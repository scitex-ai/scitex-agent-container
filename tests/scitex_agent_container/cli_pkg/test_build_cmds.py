"""Tests for ``sac image build`` (F-CS16 phase 1, F-CS17 stage 3d).

Only sdk-persistent remains as a valid target after F-CS17 deleted
the CLI/TUI surface.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.build_cmds import build, target_for_runtime


def test_target_for_runtime_container_engines():
    assert target_for_runtime("docker") == "sdk-persistent"
    assert target_for_runtime("podman") == "sdk-persistent"
    assert target_for_runtime("apptainer") == "sdk-persistent"


def test_target_for_runtime_unknown_returns_none():
    assert target_for_runtime("slurm") is None
    assert target_for_runtime("") is None
    assert target_for_runtime(None) is None


def test_build_dry_run_renders_expected_target_and_image():
    runner = CliRunner()
    result = runner.invoke(
        build,
        ["--target", "sdk-persistent", "--dry-run", "--yes"],
    )
    assert result.exit_code == 0
    assert "sdk-persistent" in result.output
    assert "scitex-agent-container:sdk-persistent" in result.output


def test_build_default_target_is_sdk_persistent():
    runner = CliRunner()
    result = runner.invoke(build, ["--dry-run", "--yes"])
    assert result.exit_code == 0
    assert "sdk-persistent" in result.output


def test_build_invokes_docker_with_dockerfile_flag(monkeypatch):
    seen = {}

    def _fake_build(image, context, dockerfile=None):
        seen.update(image=image, context=context, dockerfile=dockerfile)
        return True

    from scitex_agent_container.runtimes import docker as dock

    monkeypatch.setattr(
        dock.DockerRuntime,
        "build_image",
        classmethod(
            lambda cls, image, context, dockerfile=None: _fake_build(
                image, context, dockerfile
            )
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        build,
        ["--target", "sdk-persistent", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert seen["image"] == "scitex-agent-container:sdk-persistent"
    assert Path(seen["dockerfile"]).name == "Dockerfile.sdk-persistent"


def test_dockerfile_sdk_persistent_exists():
    """The sdk-persistent Dockerfile must ship in the repo."""
    repo = Path(__file__).resolve().parents[3]
    dockerfile = repo / "containers" / "Dockerfile.sdk-persistent"
    assert dockerfile.is_file(), f"missing: {dockerfile}"
    text = dockerfile.read_text()
    assert "claude-agent-sdk" in text
    assert "_runners.claude_session" in text
    assert "tini" in text  # signal-forwarding pid-1
