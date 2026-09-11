from __future__ import annotations

import os
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import A2ASpec
from scitex_agent_container.runtimes import _hermes_inbox_bridge_lifecycle as lifecycle


def _config(tmp_path: Path) -> AgentConfig:
    config = AgentConfig(
        name="scholar",
        harness="hermes",
        runtime="tui",
        a2a=A2ASpec(port=19001),
        config_path=str(tmp_path / "scholar" / "spec.yaml"),
    )
    return config


def test_pid_identity_requires_module_agent_and_exact_spec(tmp_path):
    proc_root = tmp_path / "proc"
    cmdline = proc_root / "42" / "cmdline"
    cmdline.parent.mkdir(parents=True)
    cmdline.write_bytes(
        b"python\0-m\0"
        + lifecycle.MODULE_PATH.encode()
        + b"\0--name\0scholar\0--config-path\0/spec/scholar.yaml\0"
    )

    assert lifecycle._owns_bridge_process(
        42,
        name="scholar",
        config_path="/spec/scholar.yaml",
        proc_root=proc_root,
    )
    assert not lifecycle._owns_bridge_process(
        42,
        name="writer",
        config_path="/spec/scholar.yaml",
        proc_root=proc_root,
    )


def test_stop_never_signals_a_foreign_reused_pid(tmp_path, monkeypatch):
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pid_path = state_dir / lifecycle.PID_FILENAME
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(lifecycle, "state_dir_for_config", lambda _config: state_dir)
    monkeypatch.setattr(lifecycle, "_owns_bridge_process", lambda *_args, **_kwargs: False)
    signals = []

    stopped = lifecycle.stop_inbox_bridge(
        config, kill=lambda pid, sig: signals.append((pid, sig))
    )

    assert stopped is False
    assert signals == []
    assert not pid_path.exists()


def test_start_preflights_auth_and_keeps_bearer_out_of_argv(tmp_path, monkeypatch):
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    seen = {}

    class Process:
        pid = 4242

    def spawn(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return Process()

    def preflight(url, bearer):
        seen["preflight"] = (url, bearer)

    monkeypatch.setattr(lifecycle, "state_dir_for_config", lambda _config: state_dir)
    monkeypatch.setattr(lifecycle, "_read_listen_bearer", lambda: "secret")
    monkeypatch.setattr(lifecycle, "listen_base_url", lambda: "http://127.0.0.1:7878")
    monkeypatch.setattr(lifecycle, "stop_inbox_bridge", lambda _config: False)

    pid = lifecycle.start_inbox_bridge(config, spawn=spawn, preflight=preflight)

    assert pid == 4242
    assert seen["preflight"] == (
        "http://127.0.0.1:7878/agents/scholar/inbox/stream?ack=explicit",
        "secret",
    )
    assert "secret" not in seen["argv"]
    assert seen["env"]["SAC_LISTEN_BEARER"] == "secret"
    assert (state_dir / lifecycle.PID_FILENAME).read_text() == "4242\n"


def test_start_fails_loud_when_subscriber_exits_immediately(tmp_path, monkeypatch):
    config = _config(tmp_path)
    state_dir = tmp_path / "state"

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(lifecycle, "state_dir_for_config", lambda _config: state_dir)
    monkeypatch.setattr(lifecycle, "_read_listen_bearer", lambda: "secret")
    monkeypatch.setattr(lifecycle, "listen_base_url", lambda: "http://127.0.0.1:7878")
    monkeypatch.setattr(lifecycle, "stop_inbox_bridge", lambda _config: False)

    try:
        lifecycle.start_inbox_bridge(
            config,
            spawn=lambda *_args, **_kwargs: Process(),
            preflight=lambda *_args: None,
        )
    except RuntimeError as exc:
        assert "exited during startup" in str(exc)
    else:
        raise AssertionError("an immediately dead subscriber must fail launch")
    assert not (state_dir / lifecycle.PID_FILENAME).exists()
