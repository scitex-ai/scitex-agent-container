"""Sealed-run argv: pure contributions + what ``build_run_argv`` renders.

Real ``AgentConfig`` + real ``build_run_argv`` (no mocks); assertions are on
the rendered argv only — nothing is launched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._sandbox import netgw_wrap, sandbox_flags, supervised_argv
from scitex_agent_container._sandbox._layout import C_NETGW
from scitex_agent_container._sandbox._spec import parse_sandbox
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_runtime import ApptainerContainerRuntime

SANDBOX = {
    "sandbox": {
        "egress": {"allow": ["opencode.ai:443", "pypi.org:443"]},
        "env": {"allow": ["KEEP_ME"], "model_key": "MODEL_KEY"},
        "run": {"root": "/scratch/someone/sac-runs", "size": "2G", "after": "remove"},
    }
}


@pytest.fixture
def sealed(tmp_path) -> tuple[AgentConfig, Path]:
    cfg = AgentConfig(
        name="sealed-a",
        runtime="tui",
        workdir=str(tmp_path / "work"),
        sandbox=parse_sandbox(SANDBOX),
    )
    return cfg, tmp_path / "state"


@pytest.fixture
def argv(sealed, tmp_path) -> list[str]:
    cfg, state = sealed
    return ApptainerContainerRuntime().build_run_argv(
        cfg, state_dir=state, sif_path=tmp_path / "x.sif"
    )


def test_network_is_none(argv):
    # Arrange
    i = argv.index("--network")
    # Act
    value = argv[i + 1]
    # Assert
    assert value == "none" and "--net" in argv


def test_writable_tmpfs_is_replaced_by_the_run_overlay(argv):
    # Arrange
    flags = set(argv)
    # Act
    has_overlay = "--overlay" in flags
    # Assert
    assert has_overlay and "--writable-tmpfs" not in flags


def test_inner_command_runs_behind_the_gateway(argv, tmp_path):
    # Arrange
    sif = argv.index(str(tmp_path / "x.sif"))
    # Act
    after = argv[sif + 1 : sif + 9]
    # Assert
    assert (
        after[:5] == ["unshare", "--user", "--map-current-user", "--net", "--keep-caps"]
        and C_NETGW in after
    )


def test_no_fleet_default_bind_reaches_a_sealed_agent(argv):
    # Arrange
    binds = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    # Act
    leaks = [
        b
        for b in binds
        if ".scitex/cards" in b or b.startswith("/tmp:") or "quota-cache" in b
    ]
    # Assert
    assert leaks == []


def test_uvwork_and_workdir_are_per_run(argv, sealed):
    # Arrange
    _, state = sealed
    run = str(state / "sandbox" / "run")
    # Act
    wd = argv[argv.index("--workdir") + 1]
    # Assert
    assert wd.startswith(run) and f"{run}/uvwork:/uvwork:rw" in argv


def test_spec_overlay_is_refused_at_argv_build(tmp_path):
    # Arrange
    from scitex_agent_container.config._types import ApptainerSpec

    cfg = AgentConfig(
        name="sealed-b",
        runtime="tui",
        workdir=str(tmp_path),
        sandbox=parse_sandbox(SANDBOX),
        apptainer=ApptainerSpec(overlay=str(tmp_path / "ov")),
    )
    # Act
    # Assert
    with pytest.raises(ValueError, match="writable layer"):
        ApptainerContainerRuntime().build_run_argv(
            cfg, state_dir=tmp_path / "s", sif_path=tmp_path / "x.sif"
        )


def test_supervised_argv_carries_policy_not_values(sealed):
    # Arrange
    cfg, state = sealed
    # Act
    out = supervised_argv(
        cfg,
        ["apptainer", "exec", "--env", "MODEL_KEY=v"],
        state_dir=state,
        image="x.sif",
    )
    # Assert
    assert out[out.index("--") + 1 :] == [
        "apptainer",
        "exec",
        "--env",
        "MODEL_KEY=v",
    ] and (out.count("--egress-allow") == 2)


def test_sandbox_flags_bind_the_tun_device(sealed):
    # Arrange
    cfg, state = sealed
    # Act
    flags = sandbox_flags(cfg, state)
    # Assert
    assert flags[flags.index("/dev/net/tun") - 1] == "--bind"


def test_netgw_wrap_keeps_the_inner_command_last():
    # Arrange
    inner = ["bash", "-c", "true"]
    # Act
    out = netgw_wrap(inner)
    # Assert
    assert out[-4:] == ["--", "bash", "-c", "true"]


def test_unsealed_spec_keeps_host_network(tmp_path):
    # Arrange
    cfg = AgentConfig(name="plain", runtime="apptainer", workdir=str(tmp_path))
    # Act
    out = ApptainerContainerRuntime().build_run_argv(
        cfg, state_dir=tmp_path / "s", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "--network" not in out and "unshare" not in out


def test_sealed_sdk_runtime_is_refused(tmp_path):
    # Arrange
    cfg = AgentConfig(
        name="sealed-sdk", runtime="apptainer", workdir=str(tmp_path), sandbox=parse_sandbox(SANDBOX)
    )
    # Act
    # Assert
    with pytest.raises(ValueError, match="TUI runtime"):
        ApptainerContainerRuntime().build_run_argv(cfg, state_dir=tmp_path / "s", sif_path=tmp_path / "x.sif")
