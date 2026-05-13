"""Tests for the A2A wiring in ApptainerContainerRuntime.

Guards the integration gap that bit ecosystem-auditor on 2026-05-13:
``spec.a2a.port`` was set in the YAML and parsed into ``A2ASpec.port``,
but ``build_run_argv`` didn't propagate it to the runner's CLI flag.
The sidecar therefore never bound, ``POST /v1/turn`` was unreachable,
and the AgentCard at ``/.well-known/agent-card.json`` 404'd.

These tests assert the exact argv shape produced when ``spec.a2a.port``
is configured, so the regression flips red if anyone drops or reorders
the wiring again.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import A2ASpec
from scitex_agent_container.runtimes._apptainer_runtime import (
    ApptainerContainerRuntime,
)


def _config_with_a2a(
    workdir: Path,
    port: int | None = None,
    config_path: str = "",
    startup_prompts: list[str] | None = None,
) -> AgentConfig:
    return AgentConfig(
        name="ecosystem-auditor",
        runtime="apptainer",
        workdir=str(workdir),
        a2a=A2ASpec(port=port) if port is not None else A2ASpec(),
        config_path=config_path,
        startup_prompts=startup_prompts or [],
    )


# ---------------------------------------------------------------------------
# spec.a2a.port → --a2a-port
# ---------------------------------------------------------------------------


def test_a2a_port_appears_in_runner_argv(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=7901, startup_prompts=["hi"])
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-port" in argv, (
        "build_run_argv must propagate spec.a2a.port to the runner; "
        "without this, the inbound sidecar never binds."
    )
    idx = argv.index("--a2a-port")
    assert argv[idx + 1] == "7901"


def test_a2a_port_omitted_when_spec_a2a_unset(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=None, startup_prompts=["hi"])
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-port" not in argv


def test_a2a_port_zero_does_not_bind(tmp_path: Path) -> None:
    """Port 0 is intentionally treated as 'no sidecar' (truthiness gate)."""
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=0, startup_prompts=["hi"])
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-port" not in argv


# ---------------------------------------------------------------------------
# --a2a-card-yaml gets the spec.yaml path
# ---------------------------------------------------------------------------


def test_a2a_card_yaml_passed_when_port_set(tmp_path: Path) -> None:
    """When a2a.port is set AND config_path is known, the runner gets
    --a2a-card-yaml pointing at the spec.yaml so it can publish the
    AgentCard at /.well-known/agent-card.json."""
    rt = ApptainerContainerRuntime()
    yaml_path = tmp_path / "agents" / "ecosystem-auditor" / "spec.yaml"
    cfg = _config_with_a2a(
        tmp_path / "wd",
        port=7901,
        config_path=str(yaml_path),
        startup_prompts=["hi"],
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-card-yaml" in argv
    idx = argv.index("--a2a-card-yaml")
    assert argv[idx + 1] == str(yaml_path)


def test_a2a_card_yaml_skipped_when_no_config_path(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(
        tmp_path / "wd", port=7901, config_path="", startup_prompts=["hi"]
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Port flag still present; just the card-yaml is skipped.
    assert "--a2a-port" in argv
    assert "--a2a-card-yaml" not in argv


def test_a2a_card_yaml_skipped_when_port_unset(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(
        tmp_path / "wd",
        port=None,
        config_path=str(tmp_path / "spec.yaml"),
        startup_prompts=["hi"],
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # No port → no sidecar → no point publishing the card path either.
    assert "--a2a-port" not in argv
    assert "--a2a-card-yaml" not in argv
