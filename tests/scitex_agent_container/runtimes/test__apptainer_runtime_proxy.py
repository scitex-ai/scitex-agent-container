"""Tests for ``kind: AgentProxy`` dispatch in ApptainerContainerRuntime.

Mirrors ``test__apptainer_runtime_a2a.py``: argv-shape-only assertions,
no live container, no subprocess execution. Guards Layer 4 of the
A2A proxy implementation — the apptainer adapter must dispatch the
inner ``python -m`` invocation by ``config.kind``:

  * Agent       → scitex_agent_container._runners.claude_session
  * AgentProxy  → scitex_agent_container._runners.a2a_proxy
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config import AgentConfig, ProxySpec
from scitex_agent_container.config._types import A2ASpec
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    RUNNER_MODULE_AGENT,
    RUNNER_MODULE_PROXY,
)
from scitex_agent_container.runtimes._apptainer_runtime import (
    ApptainerContainerRuntime,
)


def _proxy_config(
    workdir: Path,
    *,
    upstream: str = "https://peer.example.com",
    trust: str = "untrusted",
    redact: list[str] | None = None,
    timeout_s: float = 30.0,
    a2a_port: int | None = None,
    config_path: str = "",
) -> AgentConfig:
    return AgentConfig(
        name="proxy-front",
        runtime="apptainer",
        workdir=str(workdir),
        kind="AgentProxy",
        proxy=ProxySpec(
            upstream=upstream,
            trust=trust,
            redact=list(redact or []),
            timeout_s=timeout_s,
        ),
        a2a=A2ASpec(port=a2a_port) if a2a_port is not None else A2ASpec(),
        config_path=config_path,
    )


# ---------------------------------------------------------------------------
# Runner module dispatch
# ---------------------------------------------------------------------------


def test_kind_agent_proxy_dispatches_to_a2a_proxy_runner(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd")
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert RUNNER_MODULE_PROXY in argv, argv
    # And NOT the SDK runner.
    assert RUNNER_MODULE_AGENT not in argv, argv


def test_kind_agent_default_still_uses_claude_session(tmp_path: Path) -> None:
    """Sanity check the Agent path is unchanged by the dispatch refactor."""
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="agent-front",
        runtime="apptainer",
        workdir=str(tmp_path / "wd"),
        # kind defaults to "Agent"
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert RUNNER_MODULE_AGENT in argv
    assert RUNNER_MODULE_PROXY not in argv


# ---------------------------------------------------------------------------
# Proxy argv shape — required spec.proxy.* fields
# ---------------------------------------------------------------------------


def _flag_value(argv: list[str], flag: str) -> str:
    idx = argv.index(flag)
    return argv[idx + 1]


def test_proxy_upstream_trust_timeout_passed_through(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(
        tmp_path / "wd",
        upstream="https://peer.example.com",
        trust="local-mesh",
        redact=["SECRET", "TOKEN"],
        timeout_s=12.5,
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert _flag_value(argv, "--upstream") == "https://peer.example.com"
    assert _flag_value(argv, "--trust") == "local-mesh"
    assert _flag_value(argv, "--redact") == "SECRET,TOKEN"
    assert _flag_value(argv, "--timeout-s") == "12.5"
    assert _flag_value(argv, "--name") == "proxy-front"
    assert _flag_value(argv, "--state-root") == "/state"


def test_proxy_empty_redact_serialises_to_empty_string(tmp_path: Path) -> None:
    """An empty redact list still emits the flag (consistent shape)."""
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd", redact=[])
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert _flag_value(argv, "--redact") == ""


# ---------------------------------------------------------------------------
# a2a.port wiring works for the proxy runner too
# ---------------------------------------------------------------------------


def test_proxy_a2a_port_appears_when_set(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd", a2a_port=7902)
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert _flag_value(argv, "--a2a-port") == "7902"


def test_proxy_a2a_card_yaml_when_port_and_config_path_set(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    yaml_path = tmp_path / "agents" / "proxy-front" / "spec.yaml"
    cfg = _proxy_config(tmp_path / "wd", a2a_port=7902, config_path=str(yaml_path))
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert _flag_value(argv, "--a2a-card-yaml") == str(yaml_path)


def test_proxy_a2a_port_omitted_when_unset(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd", a2a_port=None)
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-port" not in argv


# ---------------------------------------------------------------------------
# Proxy MUST NOT carry SDK-only flags
# ---------------------------------------------------------------------------


def test_proxy_argv_carries_no_mission_or_autonomous_flags(tmp_path: Path) -> None:
    """The proxy runner has no SDK conversation; SDK-only flags would
    be a category error."""
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd")
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    for forbidden in (
        "--mission",
        "--autonomous-enabled",
        "--autonomous-drive-until",
        "--print-stream",
    ):
        assert forbidden not in argv, f"{forbidden!r} leaked into proxy argv"
