"""Host-side lifecycle for the harness-neutral durable inbox dispatcher."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

import scitex_logging as slogging

from .._listen._config import listen_base_url
from ..config import AgentConfig
from ._apptainer_build import _read_listen_bearer
from ._inbox_sidecar_reconcile import CURRENT_MODULE, CURRENT_ROLE
from ._tui_turn_bridge_lifecycle import resolved_a2a_port
from .tui_session import state_dir_for_config

log = slogging.getLogger(__name__)
MODULE_PATH = CURRENT_MODULE
PID_FILENAME = "channel-inbox-dispatcher.pid"
LOG_FILENAME = "channel-inbox-dispatcher.log"
PROCESS_ROLE = CURRENT_ROLE
_STOP_GRACE_S = 5.0
_CARDS_HEALTH_PROGRAM = """
import json
import sys

from scitex_cards import health

request = json.load(sys.stdin)
sys.stdout.write(json.dumps(health(**request)))
"""


def _pid_path(config: AgentConfig) -> Path:
    return state_dir_for_config(config) / PID_FILENAME


def _argv_value(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _owns_dispatcher_process(
    pid: int,
    *,
    name: str,
    config_path: str,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Prove a PID is this bridge for this agent and exact authored spec."""
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    argv = [part.decode(errors="replace") for part in raw if part]
    return (
        MODULE_PATH in argv
        and _argv_value(argv, "--name") == name
        and _argv_value(argv, "--config-path") == config_path
    )


def dispatcher_running(config: AgentConfig) -> bool:
    try:
        pid = int(_pid_path(config).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _owns_dispatcher_process(
        pid,
        name=config.name,
        config_path=str(getattr(config, "config_path", "") or ""),
    )


def stop_inbox_dispatcher(
    config: AgentConfig,
    *,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    state_dir: Path | None = None,
    owns: Callable[..., bool] = _owns_dispatcher_process,
) -> bool:
    """Stop only the identity-proven bridge recorded for this agent."""
    path = (state_dir / PID_FILENAME) if state_dir is not None else _pid_path(config)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return False
    config_path = str(getattr(config, "config_path", "") or "")
    if not owns(pid, name=config.name, config_path=config_path):
        log.warning(
            "channel inbox dispatcher PID %s is not owned by %s; not signalling",
            pid,
            config.name,
        )
        path.unlink(missing_ok=True)
        return False
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        path.unlink(missing_ok=True)
        return False
    deadline = time.monotonic() + _STOP_GRACE_S
    while time.monotonic() < deadline:
        if not owns(pid, name=config.name, config_path=config_path):
            path.unlink(missing_ok=True)
            return True
        sleep(0.05)
    if owns(pid, name=config.name, config_path=config_path):
        kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return True


def _listener_accepts_bearer(url: str, bearer: str) -> None:
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/event-stream", "Authorization": f"Bearer {bearer}"},
    )
    with urllib.request.urlopen(request, timeout=5.0) as response:
        if int(response.status) != 200:
            raise RuntimeError(f"SAC inbox stream returned HTTP {response.status}")


def cards_store_check(
    name: str,
    store: str | None,
    env: dict[str, str] | None = None,
    *,
    health: Callable[..., dict] | None = None,
):
    """Observe Cards DB readiness in the shared three-valued status shape."""
    from scitex_dev.status import Check, StatusCode

    try:
        if health is None:
            report = _cards_health_in_env(store=store, agent_id=name, env=env)
        else:
            report = health(store=store, agent_id=name)
    except Exception as exc:
        return Check.unknown(
            "cards_store_ready",
            f"Cards store readiness could not be observed ({type(exc).__name__})",
            "run `scitex-cards health --json` with this spec's environment",
            cause=StatusCode(
                kind="http",
                code=503,
                message="Cards store readiness is unknown; run scitex-cards health",
            ),
        )
    required = {"store_canonical", "store_identity", "backend_mode"}
    failed = sorted(
        str(check.get("name"))
        for check in report.get("checks", [])
        if check.get("name") in required and check.get("ok") is not True
    )
    if not failed:
        return Check.ok(
            "cards_store_ready",
            "the effective Cards store passed canonical, identity, and backend checks",
        )
    return Check.not_ok(
        "cards_store_ready",
        f"the effective Cards store failed: {', '.join(failed)}",
        "run `scitex-cards health --json` with this spec's environment, repair "
        "database/authentication, then retry",
        cause=StatusCode(
            kind="http",
            code=503,
            message="Cards store authentication/readiness failed; inspect health",
        ),
    )


def _cards_health_in_env(
    *,
    store: str | None,
    agent_id: str,
    env: dict[str, str] | None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Run Cards health under the spec env without changing this process."""
    child_env = dict(os.environ if environ is None else environ)
    child_env.update({str(key): str(value) for key, value in (env or {}).items()})
    result = subprocess.run(
        [sys.executable, "-c", _CARDS_HEALTH_PROGRAM],
        input=json.dumps({"store": store, "agent_id": agent_id}),
        text=True,
        capture_output=True,
        check=True,
        env=child_env,
    )
    report = json.loads(result.stdout)
    if not isinstance(report, dict):
        raise TypeError("scitex-cards health did not return a JSON object")
    return report


def effective_cards_store(config: AgentConfig) -> tuple[dict[str, str], str | None]:
    """Resolve the canonical environment/store shared by Cards MCP and ingress."""
    from ._board_identity_env import raw_args_env
    from ._fleet_env import effective_env

    cards_env = effective_env(config)
    cards_env.update(
        raw_args_env(getattr(getattr(config, "apptainer", None), "raw_args", None))
    )
    cards_env["SCITEX_CARDS_AGENT_ID"] = config.name
    from ._cards_ingress import canonical_store_dsn

    store = canonical_store_dsn(cards_env)
    return cards_env, store


def _cards_store_usable(
    name: str, store: str | None, env: dict[str, str] | None = None
) -> None:
    """Refuse launch unless effective Cards DB authentication is observed."""
    check = cards_store_check(name, store, env)
    if check.to_dict()["ok"] is not True:
        raise RuntimeError(
            "Cards ingress cannot authenticate/use its effective store; "
            f"{check.detail}. {check.hint}; then retry `sac agents start`."
        )


def declared_durable_channels(config: AgentConfig) -> tuple[str, ...]:
    """Return durable inbound rails from the harness-neutral declaration."""
    comms = getattr(config, "comms", None)
    authored = getattr(comms, "channels", None)
    if authored is None:
        authored = getattr(getattr(config, "claude", None), "channels", [])
    supported = {"server:sac", "server:scitex-cards"}
    return tuple(
        dict.fromkeys(
            str(channel).strip()
            for channel in (authored or [])
            if str(channel).strip() in supported
        )
    )


def start_inbox_dispatcher(
    config: AgentConfig,
    *,
    spawn: Callable[..., Any] = subprocess.Popen,
    preflight: Callable[[str, str], None] = _listener_accepts_bearer,
    cards_preflight: Callable[[str, str | None, dict[str, str]], None] = (
        _cards_store_usable
    ),
    bearer: str | None = None,
    base_url: str | None = None,
    state_dir: Path | None = None,
    stop: Callable[..., bool] = stop_inbox_dispatcher,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Start one explicit-ack consumer after its declared rails are reachable."""
    channels = declared_durable_channels(config)
    if not channels:
        return 0
    port = resolved_a2a_port(config)
    config_path = str(getattr(config, "config_path", "") or "")
    if port is None:
        raise RuntimeError("channel inbox delivery requires a resolved spec.a2a.port")
    if not config_path:
        raise RuntimeError("channel inbox delivery requires config.config_path")
    bearer = bearer or _read_listen_bearer()
    if not bearer:
        raise RuntimeError(
            "SAC listen bearer is absent; refusing a deaf channel dispatcher launch"
        )
    # The dispatcher is host-side, while the matching Cards MCP tools are inside
    # Apptainer. Give both the SAME effective store identity; inheriting only
    # the operator shell made Cards delivery depend on which shell launched
    # SAC and could poll a different/absent store.
    cards_env: dict[str, str] = {}
    cards_store: str | None = None
    if "server:scitex-cards" in channels:
        cards_env, cards_store = effective_cards_store(config)
        cards_preflight(config.name, cards_store, cards_env)
    stop(config)
    base_url = (base_url or listen_base_url()).rstrip("/")
    stream_url = f"{base_url}/agents/{config.name}/inbox/stream?ack=explicit"
    preflight(stream_url, bearer)
    state_dir = state_dir or state_dir_for_config(config)
    state_dir.mkdir(parents=True, exist_ok=True)
    incarnation_id = str(config.env.get("SAC_INSTANCE_UUID") or uuid.uuid4())
    config.env["SAC_INSTANCE_UUID"] = incarnation_id
    argv = [
        sys.executable,
        "-m",
        MODULE_PATH,
        "--name",
        config.name,
        "--listen-url",
        base_url,
        "--turn-url",
        f"http://127.0.0.1:{port}/v1/turn",
        "--config-path",
        config_path,
        "--process-role",
        PROCESS_ROLE,
        "--incarnation-id",
        incarnation_id,
        "--state-dir",
        str(state_dir),
    ]
    for channel in channels:
        argv += ["--channel", channel]
    env = os.environ.copy()
    env.pop("SCITEX_CARDS_DB", None)
    env["SAC_LISTEN_BEARER"] = bearer
    env["SAC_INSTANCE_UUID"] = incarnation_id
    env["SAC_PROCESS_ROLE"] = PROCESS_ROLE
    # scitex-cards keeps notification-backend selection explicit: the task
    # store argument passed to poll_notifications identifies the data store,
    # but the inbox transport itself is selected from SCITEX_CARDS_INBOX_DSN.
    # Bind that rail to the same canonical Store DSN already validated above.
    # Do not restore SCITEX_CARDS_DB: it is a legacy, broader alias which can
    # silently select a second task store from the launching shell.
    if cards_store is not None:
        env["SCITEX_CARDS_INBOX_DSN"] = cards_store
    for key in (
        "SCITEX_CARDS_AGENT_ID",
        "SCITEX_CARDS_NOTIFY_DSN",
        "SCITEX_STORE_DSN",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSFILE",
    ):
        value = cards_env.get(key)
        if value is not None:
            env[key] = str(value)
    with open(state_dir / LOG_FILENAME, "ab") as output:
        process = spawn(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        raise RuntimeError("channel inbox dispatcher spawn returned no PID")
    pid_path = state_dir / PID_FILENAME
    pid_path.write_text(f"{pid}\n", encoding="utf-8")
    sleep(0.2)
    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        pid_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"channel inbox dispatcher exited during startup; inspect "
            f"{state_dir / LOG_FILENAME}"
        )
    return pid


__all__ = [
    "LOG_FILENAME",
    "MODULE_PATH",
    "PID_FILENAME",
    "declared_durable_channels",
    "dispatcher_running",
    "start_inbox_dispatcher",
    "stop_inbox_dispatcher",
]
