"""Tests for ``sac image build`` (layered :base / :scitex)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.build_cmds import build, target_for_runtime


def test_target_for_runtime_container_engines():
    assert target_for_runtime("docker") == "scitex"
    assert target_for_runtime("podman") == "scitex"
    assert target_for_runtime("apptainer") == "scitex"


def test_target_for_runtime_unknown_returns_none():
    assert target_for_runtime("slurm") is None
    assert target_for_runtime("") is None
    assert target_for_runtime(None) is None


def test_build_dry_run_renders_expected_target_and_image():
    runner = CliRunner()
    result = runner.invoke(
        build,
        ["--target", "scitex", "--dry-run", "--yes"],
    )
    assert result.exit_code == 0
    assert "scitex" in result.output
    assert "scitex-agent-container:scitex" in result.output


def test_build_default_target_is_scitex():
    runner = CliRunner()
    result = runner.invoke(build, ["--dry-run", "--yes"])
    assert result.exit_code == 0
    assert "scitex" in result.output


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
        ["--target", "scitex", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert seen["image"] == "scitex-agent-container:scitex"
    assert Path(seen["dockerfile"]).name == "Dockerfile.scitex"


def test_dockerfile_scitex_exists():
    """The Dockerfile.scitex (default layer) must ship in the wheel."""
    import scitex_agent_container

    pkg = Path(scitex_agent_container.__file__).resolve().parent
    dockerfile = pkg / "containers" / "Dockerfile.scitex"
    assert dockerfile.is_file(), f"missing: {dockerfile}"
    text = dockerfile.read_text()
    assert "claude-agent-sdk" in text
    assert "_runners.claude_session" in text


def test_dockerfile_base_exists():
    """The Dockerfile.base (foundation layer) must ship in the wheel."""
    import scitex_agent_container

    pkg = Path(scitex_agent_container.__file__).resolve().parent
    dockerfile = pkg / "containers" / "Dockerfile.base"
    assert dockerfile.is_file(), f"missing: {dockerfile}"
