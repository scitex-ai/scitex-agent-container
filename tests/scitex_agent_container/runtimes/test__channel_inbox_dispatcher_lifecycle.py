from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._acl_types import CommsSpec
from scitex_agent_container.config._types import A2ASpec
from scitex_agent_container.runtimes import (
    _channel_inbox_dispatcher_lifecycle as lifecycle,
)


def _config(tmp_path: Path) -> AgentConfig:
    config = AgentConfig(
        name="scholar",
        harness="hermes",
        runtime="tui",
        a2a=A2ASpec(port=19001),
        comms=CommsSpec(channels=["server:sac", "server:scitex-cards"]),
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
        lifecycle._owns_dispatcher_process(
            42,
            name="scholar",
            config_path="/spec/scholar.yaml",
            proc_root=proc_root,
        ),
        lifecycle._owns_dispatcher_process(
            42,
            name="writer",
            config_path="/spec/scholar.yaml",
            proc_root=proc_root,
        ),
    )
    # Assert
    assert ownership == (True, False)


@pytest.mark.parametrize("harness", ["claude-code", "hermes", "codex"])
def test_durable_channel_selection_is_harness_independent(tmp_path, harness):
    # Arrange
    config = _config(tmp_path)
    config.harness = harness
    # Act
    selected = lifecycle.declared_durable_channels(config)
    # Assert
    assert selected == (
        "server:sac",
        "server:scitex-cards",
    )


def test_no_durable_channels_starts_no_dispatcher(tmp_path):
    # Arrange
    config = _config(tmp_path)
    config.comms.channels = ["server:claude-code-telegrammer"]
    spawned = []
    # Act
    started = lifecycle.start_inbox_dispatcher(
        config, spawn=lambda *a, **k: spawned.append((a, k))
    )
    # Assert
    assert (started, spawned) == (0, [])


def test_stop_never_signals_a_foreign_reused_pid(tmp_path):
    # Arrange
    config = _config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pid_path = state_dir / lifecycle.PID_FILENAME
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    signals = []
    # Act
    stopped = lifecycle.stop_inbox_dispatcher(
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
    config.env["SAC_INSTANCE_UUID"] = "inc-4242"
    config.env["SCITEX_STORE_DSN"] = "postgresql://cards-primary:55432/cards"
    config.env["SCITEX_CARDS_NOTIFY_DSN"] = "postgresql://cards-primary:55433/cards"
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

    def cards_preflight(name, store, env):
        seen["cards_preflight"] = (name, store, env)

    # Act
    pid = lifecycle.start_inbox_dispatcher(
        config,
        spawn=spawn,
        preflight=preflight,
        cards_preflight=cards_preflight,
        bearer="secret",
        base_url="http://127.0.0.1:7878",
        state_dir=state_dir,
        stop=lambda _config: False,
        sleep=lambda _seconds: None,
    )
    outcome = (
        pid,
        seen["preflight"],
        seen["cards_preflight"][:2],
        seen["cards_preflight"][2]["PGUSER"],
        "secret" in seen["argv"],
        seen["env"]["SAC_LISTEN_BEARER"],
        seen["env"]["SAC_INSTANCE_UUID"],
        seen["env"]["SAC_PROCESS_ROLE"],
        seen["argv"][seen["argv"].index("--incarnation-id") + 1],
        seen["argv"][seen["argv"].index("--process-role") + 1],
        seen["env"]["SCITEX_CARDS_AGENT_ID"],
        seen["env"]["SCITEX_STORE_DSN"],
        seen["env"]["SCITEX_CARDS_INBOX_DSN"],
        seen["env"]["SCITEX_CARDS_NOTIFY_DSN"],
        (state_dir / lifecycle.PID_FILENAME).read_text(),
    )
    # Assert
    assert outcome == (
        4242,
        (
            "http://127.0.0.1:7878/agents/scholar/inbox/stream?ack=explicit",
            "secret",
        ),
        ("scholar", "postgresql://cards-primary:55432/cards"),
        seen["env"]["PGUSER"],
        False,
        "secret",
        "inc-4242",
        lifecycle.PROCESS_ROLE,
        "inc-4242",
        lifecycle.PROCESS_ROLE,
        "scholar",
        "postgresql://cards-primary:55432/cards",
        "postgresql://cards-primary:55432/cards",
        "postgresql://cards-primary:55433/cards",
        "4242\n",
    )


def test_start_drops_inherited_cards_store_alias(tmp_path, env_save_restore):
    # Arrange
    config = _config(tmp_path)
    canonical = "postgresql://scitex-primary:55432/scitex"
    config.env["SCITEX_STORE_DSN"] = canonical
    env_save_restore.set("SCITEX_CARDS_DB", "postgresql://wrong:55432/private")
    seen = {}

    class Process:
        pid = 4242

    def spawn(_argv, **kwargs):
        seen["env"] = kwargs["env"]
        return Process()

    # Act
    lifecycle.start_inbox_dispatcher(
        config,
        spawn=spawn,
        preflight=lambda *_args: None,
        cards_preflight=lambda *_args: None,
        bearer="secret",
        state_dir=tmp_path / "state",
        stop=lambda _config: False,
        sleep=lambda _seconds: None,
    )

    # Assert
    assert (
        seen["env"]["SCITEX_STORE_DSN"],
        seen["env"]["SCITEX_CARDS_INBOX_DSN"],
        "SCITEX_CARDS_DB" in seen["env"],
    ) == (canonical, canonical, False)


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
        lifecycle.start_inbox_dispatcher(
            config,
            spawn=lambda *_args, **_kwargs: Process(),
            preflight=lambda *_args: None,
            cards_preflight=lambda *_args: None,
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


def test_start_refuses_when_cards_store_preflight_fails(tmp_path):
    # Arrange
    config = _config(tmp_path)
    spawned = []

    def refuse(_name, _store, _env):
        raise RuntimeError("Cards database authentication failed")

    # Act
    try:
        lifecycle.start_inbox_dispatcher(
            config,
            spawn=lambda *args, **kwargs: spawned.append((args, kwargs)),
            preflight=lambda *_args: None,
            cards_preflight=refuse,
            bearer="secret",
            state_dir=tmp_path / "state",
            stop=lambda _config: False,
        )
    except RuntimeError as exc:
        error = exc
    else:
        error = None

    # Assert
    assert (str(error), spawned) == ("Cards database authentication failed", [])


def test_cards_store_check_reports_actionable_native_unavailable():
    # Arrange
    def health(**_kwargs):
        return {
            "checks": [
                {
                    "name": "store_identity",
                    "ok": False,
                    "detail": "authentication rejected",
                    "hint": "repair credentials",
                }
            ]
        }

    # Act
    check = lifecycle.cards_store_check(
        "scitex-hub", "postgresql://redacted", health=health
    )

    # Assert
    assert (
        check.to_dict()["ok"],
        check.name,
        check.cause.kind,
        check.cause.code,
        "health" in check.hint,
    ) == (False, "cards_store_ready", "http", 503, True)


def test_cards_store_check_preserves_unknown_when_observation_raises():
    # Arrange
    def unavailable(**_kwargs):
        raise RuntimeError("postgres://user:secret@host/db")

    # Act
    check = lifecycle.cards_store_check(
        "scitex-hub", "postgresql://redacted", health=unavailable
    )

    # Assert: type only, never the credential-bearing exception text.
    assert (
        check.to_dict()["ok"],
        check.cause.code,
        "secret" not in check.detail,
    ) == (None, 503, True)


def test_cards_health_uses_effective_env_for_backend_mode_without_leaking(tmp_path):
    # Arrange: a tiny stand-in records exactly what backend_mode would read.
    package = tmp_path / "scitex_cards"
    package.mkdir()
    (package / "__init__.py").write_text(
        """import os

def health(*, store, agent_id):
    observed = (
        os.environ.get("SCITEX_STORE_DSN"),
        os.environ.get("SCITEX_CARDS_AGENT_ID"),
        os.environ.get("PGUSER"),
    )
    expected = (store, agent_id, "spec_role")
    return {"checks": [{"name": "backend_mode", "ok": observed == expected,
                        "detail": repr(observed), "hint": None}]}
""",
        encoding="utf-8",
    )
    host_env = {
        "PATH": os.environ["PATH"],
        "SCITEX_STORE_DSN": "postgresql://host-default:55432/cards",
        "SCITEX_CARDS_AGENT_ID": "host-agent",
        "PGUSER": "host_role",
    }
    host_before = dict(host_env)
    spec_store = "postgresql://spec-primary:55432/cards"
    spec_env = {
        "PYTHONPATH": str(tmp_path),
        "SCITEX_STORE_DSN": spec_store,
        "SCITEX_CARDS_AGENT_ID": "scholar",
        "PGUSER": "spec_role",
    }

    # Act
    report = lifecycle._cards_health_in_env(
        store=spec_store,
        agent_id="scholar",
        env=spec_env,
        environ=host_env,
    )

    # Assert: backend_mode saw the spec values; the parent retained host values.
    assert (report["checks"][0]["ok"], host_env) == (True, host_before)
