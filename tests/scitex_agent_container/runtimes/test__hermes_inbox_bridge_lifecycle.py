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
    # Arrange
    proc_root = tmp_path / "proc"
    cmdline = proc_root / "42" / "cmdline"
    cmdline.parent.mkdir(parents=True)
    cmdline.write_bytes(
        b"python\0-m\0"
        + lifecycle.MODULE_PATH.encode()
        + b"\0--name\0scholar\0--config-path\0/spec/scholar.yaml\0"
    )

    # Act
    ownership = (
        lifecycle._owns_bridge_process(
            42,
            name="scholar",
            config_path="/spec/scholar.yaml",
            proc_root=proc_root,
        ),
        lifecycle._owns_bridge_process(
            42,
            name="writer",
            config_path="/spec/scholar.yaml",
            proc_root=proc_root,
        ),
    )
    # Assert
    assert ownership == (True, False)


def test_stop_never_signals_a_foreign_reused_pid(tmp_path):
    # Arrange
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pid_path = state_dir / lifecycle.PID_FILENAME
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    signals = []
    # Act
    stopped = lifecycle.stop_inbox_bridge(
        config,
        kill=lambda pid, sig: signals.append((pid, sig)),
        state_dir=state_dir,
        owns=lambda *_args, **_kwargs: False,
    )
    # Assert
    assert (stopped, signals, pid_path.exists()) == (False, [], False)


def test_start_preflights_auth_and_keeps_bearer_out_of_argv(tmp_path):
    # Arrange
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

    # Act
    pid = lifecycle.start_inbox_bridge(
        config,
        spawn=spawn,
        preflight=preflight,
        bearer="secret",
        base_url="http://127.0.0.1:7878",
        state_dir=state_dir,
        stop=lambda _config: False,
        sleep=lambda _seconds: None,
    )
    outcome = (
        pid,
        seen["preflight"],
        "secret" in seen["argv"],
        seen["env"]["SAC_LISTEN_BEARER"],
        (state_dir / lifecycle.PID_FILENAME).read_text(),
    )
    # Assert
    assert outcome == (
        4242,
        (
            "http://127.0.0.1:7878/agents/scholar/inbox/stream?ack=explicit",
            "secret",
        ),
        False,
        "secret",
        "4242\n",
    )


def test_start_fails_loud_when_subscriber_exits_immediately(tmp_path):
    # Arrange
    config = _config(tmp_path)
    state_dir = tmp_path / "state"

    class Process:
        pid = 4242

        @staticmethod
        def poll():
            return 1

    error = None
    # Act
    try:
        lifecycle.start_inbox_bridge(
            config,
            spawn=lambda *_args, **_kwargs: Process(),
            preflight=lambda *_args: None,
            bearer="secret",
            base_url="http://127.0.0.1:7878",
            state_dir=state_dir,
            stop=lambda _config: False,
            sleep=lambda _seconds: None,
        )
    except RuntimeError as exc:
        error = exc
    # Assert
    assert (
        type(error),
        "exited during startup" in str(error),
        (state_dir / lifecycle.PID_FILENAME).exists(),
    ) == (
        RuntimeError,
        True,
        False,
    )
