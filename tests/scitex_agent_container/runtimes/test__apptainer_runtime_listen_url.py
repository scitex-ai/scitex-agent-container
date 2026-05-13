"""Layer-5 of auto-port-allocation: SAC_LISTEN_BASE_URL injection.

The apptainer runtime must forward the host-stable ``sac listen`` base
URL into every container as ``SAC_LISTEN_BASE_URL``. The per-agent
sidecar's ``/.well-known/agent-card.json`` handler reads that env var
and uses it as the AgentCard's ``url`` — so cards stay stable across
auto-port-allocator restarts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._apptainer_runtime import (
    ApptainerContainerRuntime,
)


def _config(workdir: Path) -> AgentConfig:
    return AgentConfig(name="alpha", runtime="apptainer", workdir=str(workdir))


def _env_pairs(argv: list[str]) -> dict[str, str]:
    """Decode every ``--env KEY=VAL`` pair in the argv into a dict."""
    out: dict[str, str] = {}
    for i, a in enumerate(argv):
        if a == "--env" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
    return out


class TestSacListenBaseURLInjection:
    def test_default_listen_url_injected(self, tmp_path: Path) -> None:
        """No config.yaml present → defaults to http://127.0.0.1:7878."""
        rt = ApptainerContainerRuntime()
        cfg = _config(tmp_path)
        argv = rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
        envs = _env_pairs(argv)
        assert envs.get("SAC_LISTEN_BASE_URL") == "http://127.0.0.1:7878"

    def test_config_listen_port_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``listen.port: 9090`` in config.yaml flows into the env var."""
        cfg_yaml = tmp_path / "config.yaml"
        cfg_yaml.write_text("listen:\n  port: 9090\n")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_yaml))
        rt = ApptainerContainerRuntime()
        cfg = _config(tmp_path)
        argv = rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
        envs = _env_pairs(argv)
        assert envs.get("SAC_LISTEN_BASE_URL") == "http://127.0.0.1:9090"

    def test_config_listen_host_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``listen.host`` override is honoured too — operators on tailscale
        binds want their card url to advertise the public tunnel ip."""
        cfg_yaml = tmp_path / "config.yaml"
        cfg_yaml.write_text("listen:\n  host: 100.64.1.2\n  port: 7878\n")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_yaml))
        rt = ApptainerContainerRuntime()
        cfg = _config(tmp_path)
        argv = rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
        envs = _env_pairs(argv)
        assert envs.get("SAC_LISTEN_BASE_URL") == "http://100.64.1.2:7878"
