"""Tests for ``sac image build`` (F-CS16 phase 1).

Covers the new ``--target {cli-tui,sdk-persistent}`` flag, the
``--dockerfile`` plumbing through to DockerRuntime.build_image, and
the runtime->target mapping helper.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.build_cmds import build, target_for_runtime


def test_target_for_runtime_canonical_names():
    assert target_for_runtime("claude-code") == "cli-tui"
    assert target_for_runtime("claude-session") == "sdk-persistent"


def test_target_for_runtime_f_cs6_aliases():
    """F-CS6 yaml-friendly aliases must map to the same target."""
    assert target_for_runtime("claude-cli-tui") == "cli-tui"
    assert target_for_runtime("claude-sdk-persistent") == "sdk-persistent"


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


def test_build_default_target_is_cli_tui_for_backwards_compat():
    runner = CliRunner()
    result = runner.invoke(build, ["--dry-run", "--yes"])
    assert result.exit_code == 0
    # Default --target is cli-tui; default --image is :cli-tui.
    assert "cli-tui" in result.output


def test_build_invokes_docker_with_dockerfile_flag(monkeypatch):
    """When --target sdk-persistent is selected, DockerRuntime.build_image
    must receive the Dockerfile.sdk-persistent path."""
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


def test_build_apptainer_sdk_persistent_not_yet_supported(monkeypatch):
    runner = CliRunner()
    result = runner.invoke(
        build,
        [
            "--runtime",
            "apptainer",
            "--target",
            "sdk-persistent",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "not wired" in result.output.lower()


def test_dockerfile_sdk_persistent_exists():
    """The sdk-persistent Dockerfile must ship in the repo."""
    repo = Path(__file__).resolve().parents[3]
    dockerfile = repo / "containers" / "Dockerfile.sdk-persistent"
    assert dockerfile.is_file(), f"missing: {dockerfile}"
    text = dockerfile.read_text()
    assert "claude-agent-sdk" in text
    assert "_runners.claude_session" in text
    assert "tini" in text  # signal-forwarding pid-1
