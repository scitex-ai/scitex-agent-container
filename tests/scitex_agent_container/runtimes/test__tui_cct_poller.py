"""Hermes owns one explicit CCT poller for the lifetime of its TUI."""

from __future__ import annotations

import json
import signal
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._tui_cct_poller import (
    PID_FILENAME,
    TuiCctPollerError,
    _poller_env,
    _preflight_cct,
    start_cct_poller,
    stop_cct_poller,
)


def _config() -> AgentConfig:
    config = AgentConfig(name="lead", harness="hermes", runtime="tui")
    config.a2a.port = 19003
    config.claude.channels = ["server:claude-code-telegrammer"]
    return config


def _materialize_cct(state: Path) -> tuple[Path, Path]:
    home = state / "home"
    home.mkdir(parents=True)
    bun = state / "bun"
    bun.write_text("#!/bin/sh\n", encoding="utf-8")
    bun.chmod(0o700)
    server = state / "telegram-server.ts"
    poller = state / "telegram-poller.ts"
    server.write_text("", encoding="utf-8")
    poller.write_text("", encoding="utf-8")
    (home / ".env").write_text(
        "CCT_BOT_TOKEN=test-token\nCCT_ALLOWED_USERS=123\n", encoding="utf-8"
    )
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "claude-code-telegrammer": {
                        "command": str(bun),
                        "args": ["run", str(server)],
                        "env": {
                            "CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}",
                            "CCT_ALLOWED_USERS": "${env:CCT_ALLOWED_USERS}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return bun, poller


class _Process:
    pid = 4242

    @staticmethod
    def poll():
        return None


class _Spawner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return _Process()


def test_start_derives_standalone_poller_and_exact_hermes_turn_url(tmp_path):
    # Arrange
    config = _config()
    bun, poller = _materialize_cct(tmp_path)
    spawner = _Spawner()
    stopped = []

    def stop(_config, **kwargs):
        stopped.append(kwargs["state_dir"])
        return False

    # Act
    preflights = []
    pid = start_cct_poller(
        config,
        state_dir=tmp_path,
        spawn=spawner,
        stop=stop,
        preflight=lambda entry, env: preflights.append((entry, env)),
        sleep=lambda _seconds: None,
    )

    # Assert
    argv, options = spawner.calls[0]
    env = options["env"]
    assert (
        pid,
        argv,
        len(spawner.calls),
        stopped,
        len(preflights),
        env["SAC_NAME"],
        env["CLAUDE_CODE_TELEGRAMMER_EXTERNAL_POLLER"],
        env["CCT_HARNESS"],
        env["CCT_TURN_URL"],
        env["CLAUDE_CODE_TELEGRAMMER_TURN_URL"],
        env["CCT_AGENT_ID"],
        (tmp_path / PID_FILENAME).read_text(encoding="utf-8"),
    ) == (
        4242,
        [str(bun), "run", str(poller)],
        1,
        [tmp_path],
        1,
        "lead",
        "1",
        "hermes-tui",
        "http://127.0.0.1:19003/v1/turn",
        "http://127.0.0.1:19003/v1/turn",
        "lead",
        "4242\n",
    )


def test_start_is_noop_when_telegram_channel_is_not_selected(tmp_path):
    # Arrange
    config = _config()
    config.claude.channels = []
    spawner = _Spawner()
    # Act
    pid = start_cct_poller(config, state_dir=tmp_path, spawn=spawner)
    # Assert
    assert (pid, spawner.calls) == (None, [])


def test_poller_env_preserves_current_cct_names_and_scrubs_retired_names(
    tmp_path, env_save_restore
):
    # Arrange
    config = _config()
    _materialize_cct(tmp_path)
    env_save_restore.set("CLAUDE_CODE_TELEGRAMMER_TELEGRAM_BOT_TOKEN", "retired")
    env_save_restore.set("CLAUDE_CODE_TELEGRAMMER_TELEGRAM_ALLOWED_USERS", "old")
    entry = json.loads(
        (tmp_path / "home" / ".mcp.json").read_text(encoding="utf-8")
    )["mcpServers"]["claude-code-telegrammer"]
    # Act
    env = _poller_env(config, home=tmp_path / "home", entry=entry)
    # Assert
    assert (
        env.get("CCT_BOT_TOKEN"),
        env.get("CCT_ALLOWED_USERS"),
        "CLAUDE_CODE_TELEGRAMMER_TELEGRAM_BOT_TOKEN" in env,
        "CLAUDE_CODE_TELEGRAMMER_TELEGRAM_ALLOWED_USERS" in env,
    ) == ("test-token", "123", False, False)


def test_start_refuses_before_spawn_when_cct_preflight_fails(tmp_path):
    # Arrange
    config = _config()
    _materialize_cct(tmp_path)
    spawner = _Spawner()

    def fail(_entry, _env):
        raise RuntimeError("missing project principal")

    # Act
    try:
        start_cct_poller(
            config,
            state_dir=tmp_path,
            spawn=spawner,
            preflight=fail,
        )
    except RuntimeError as exc:
        error = str(exc)
    # Assert
    assert (error, spawner.calls) == ("missing project principal", [])


def test_preflight_fails_loud_on_unusable_store_identity(tmp_path):
    # Arrange
    bun, _poller = _materialize_cct(tmp_path)
    entry = json.loads(
        (tmp_path / "home" / ".mcp.json").read_text(encoding="utf-8")
    )["mcpServers"]["claude-code-telegrammer"]
    report = {
        "checks": [
            {"name": "bot_token_valid", "ok": True},
            {"name": "db_schema_current", "ok": False},
            {"name": "poller_alive", "ok": False},
        ]
    }
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return type("Result", (), {"stdout": json.dumps(report)})()

    # Act
    try:
        _preflight_cct(entry, {"PGUSER": "operator__lead"}, run=run)
    except TuiCctPollerError as exc:
        error = str(exc)
    # Assert
    assert (
        calls,
        "db_schema_current" in error,
        "poller_alive" in error,
        "operator__lead" in error,
    ) == (
        [[str(bun), "run", str(tmp_path / "telegram-server.ts"), "health"]],
        True,
        False,
        True,
    )


def test_preflight_accepts_healthy_dependencies_before_poller_exists(tmp_path):
    # Arrange
    _bun, _poller = _materialize_cct(tmp_path)
    entry = json.loads(
        (tmp_path / "home" / ".mcp.json").read_text(encoding="utf-8")
    )["mcpServers"]["claude-code-telegrammer"]
    checks = [
        {"name": name, "ok": True}
        for name in (
            "bot_token_present",
            "bot_token_valid",
            "allowlist_nonempty",
            "state_dir_writable",
            "db_schema_current",
            "wake_target_reachable",
        )
    ]
    checks.append({"name": "poller_alive", "ok": False})

    # Act
    result = _preflight_cct(
        entry,
        {"PGUSER": "operator__lead"},
        run=lambda *_args, **_kwargs: type(
            "Result", (), {"stdout": json.dumps({"checks": checks})}
        )(),
    )
    # Assert
    assert result is None


def test_stop_signals_only_owned_poller_and_removes_managed_pidfile(tmp_path):
    # Arrange
    config = _config()
    path = tmp_path / PID_FILENAME
    path.write_text("4242\n", encoding="utf-8")
    signals = []
    observations = iter((True, False))
    ownership_calls = []

    def owns(pid, *, name):
        ownership_calls.append((pid, name))
        return next(observations)

    # Act
    stopped = stop_cct_poller(
        config,
        state_dir=tmp_path,
        owns=owns,
        poller_pids=lambda _name: (),
        kill=lambda pid, sig: signals.append((pid, sig)),
        sleep=lambda _seconds: None,
    )

    # Assert
    assert (stopped, signals, path.exists(), ownership_calls) == (
        True,
        [(4242, signal.SIGTERM)],
        False,
        [(4242, "lead"), (4242, "lead")],
    )


def test_stop_refuses_reused_unowned_pid(tmp_path):
    # Arrange
    config = _config()
    path = tmp_path / PID_FILENAME
    path.write_text("4242\n", encoding="utf-8")
    signals = []
    # Act
    stopped = stop_cct_poller(
        config,
        state_dir=tmp_path,
        owns=lambda pid, *, name: False,
        poller_pids=lambda _name: (),
        kill=lambda pid, sig: signals.append((pid, sig)),
    )
    # Assert
    assert (stopped, signals, path.exists()) == (False, [], False)


def test_stop_cleans_successor_poller_even_when_managed_pidfile_is_stale(tmp_path):
    # Arrange — CCT's newest-wins takeover replaced SAC's originally recorded
    # PID. Lifecycle cleanup must follow agent identity, not the stale number.
    config = _config()
    path = tmp_path / PID_FILENAME
    path.write_text("1111\n", encoding="utf-8")
    signals = []
    live = {2222}
    ownership_calls = []

    def owns(pid, *, name):
        ownership_calls.append((pid, name))
        return pid in live

    def kill(pid, sig):
        signals.append((pid, sig))
        live.discard(pid)

    # Act
    stopped = stop_cct_poller(
        config,
        state_dir=tmp_path,
        owns=owns,
        poller_pids=lambda _name: (2222,),
        kill=kill,
        sleep=lambda _seconds: None,
    )
    # Assert
    assert (stopped, signals, path.exists(), ownership_calls) == (
        True,
        [(2222, signal.SIGTERM)],
        False,
        [(1111, "lead"), (2222, "lead")],
    )
