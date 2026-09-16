"""Lifecycle-owned Telegram poller for the Hermes TUI.

Hermes can use the CCT MCP server for outbound tools, but an MCP server is not
an inbound lifecycle primitive: Hermes may start it lazily, recycle it, or
never invoke it.  SAC therefore starts CCT's standalone ``telegram-poller.ts``
beside the host-side Hermes turn bridge and tears it down with the TUI.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..config import AgentConfig
from ._hermes_cct import cct_requested
from ._sdk_channels import _TELEGRAMMER_MCP_KEY
from ._tui_turn_bridge_lifecycle import resolved_a2a_port
from .tui_session import state_dir_for_config

log = logging.getLogger(__name__)

PID_FILENAME = "hermes-cct-poller.pid"
LOG_FILENAME = "hermes-cct-poller.log"
_POLLER_NAME = "telegram-poller.ts"
_SERVER_NAME = "telegram-server.ts"
_STOP_GRACE_S = 5.0
_START_SETTLE_S = 0.2
_PLACEHOLDER = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}$")
_STARTUP_HEALTH_CHECKS = frozenset(
    {
        "bot_token_present",
        "bot_token_valid",
        "allowlist_nonempty",
        "state_dir_writable",
        "db_schema_current",
        "wake_target_reachable",
    }
)


class HermesCctPollerError(RuntimeError):
    """The selected Hermes Telegram poller cannot be launched safely."""


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value
    return values


def _mcp_entry(home: Path) -> dict[str, Any]:
    path = home / ".mcp.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HermesCctPollerError(
            f"cannot read Hermes CCT MCP entry at {path}: {exc}"
        ) from exc
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    entry = servers.get(_TELEGRAMMER_MCP_KEY) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        raise HermesCctPollerError(
            f"Hermes CCT channel is selected, but {path} has no "
            f"{_TELEGRAMMER_MCP_KEY!r} MCP entry"
        )
    return entry


def _poller_argv(entry: dict[str, Any]) -> list[str]:
    """Translate the declared CCT server command to its standalone poller."""
    command = str(entry.get("command", "") or "").strip()
    args = [str(value) for value in (entry.get("args") or ())]
    executable = command if Path(command).is_file() else shutil.which(command)
    if not executable:
        raise HermesCctPollerError(f"CCT executable is unavailable: {command!r}")
    for index, value in enumerate(args):
        candidate = Path(value).expanduser()
        if candidate.name != _SERVER_NAME:
            continue
        poller = candidate.with_name(_POLLER_NAME)
        if not poller.is_file():
            raise HermesCctPollerError(
                f"CCT standalone poller is absent beside declared server: {poller}"
            )
        return [str(executable), *args[:index], str(poller), *args[index + 1 :]]
    raise HermesCctPollerError(
        f"CCT MCP entry does not name {_SERVER_NAME}; cannot derive {_POLLER_NAME}"
    )


def _health_argv(entry: dict[str, Any]) -> list[str]:
    """Return CCT's authoritative, side-effect-free health command."""
    command = str(entry.get("command", "") or "").strip()
    args = [str(value) for value in (entry.get("args") or ())]
    executable = command if Path(command).is_file() else shutil.which(command)
    if not executable:
        raise HermesCctPollerError(f"CCT executable is unavailable: {command!r}")
    if not any(Path(value).name == _SERVER_NAME for value in args):
        raise HermesCctPollerError(
            f"CCT MCP entry does not name {_SERVER_NAME}; cannot run health preflight"
        )
    return [str(executable), *args, "health"]


def _preflight_cct(
    entry: dict[str, Any],
    env: dict[str, str],
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Refuse a poller whose token, Store identity, or turn URL is unusable."""
    result = run(
        _health_argv(entry),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    try:
        report = json.loads(str(getattr(result, "stdout", "") or ""))
    except ValueError as exc:
        raise HermesCctPollerError(
            "CCT health preflight returned no valid JSON; reinstall "
            "claude-code-telegrammer and retry the agent start"
        ) from exc
    checks = report.get("checks") if isinstance(report, dict) else None
    if not isinstance(checks, list):
        raise HermesCctPollerError(
            "CCT health preflight returned no checks; reinstall "
            "claude-code-telegrammer and retry the agent start"
        )
    failed = [
        str(item.get("name"))
        for item in checks
        if isinstance(item, dict)
        and item.get("name") in _STARTUP_HEALTH_CHECKS
        and item.get("ok") is not True
    ]
    if failed:
        role = str(env.get("PGUSER", "") or "<libpq default>")
        raise HermesCctPollerError(
            "CCT startup preflight failed "
            f"({', '.join(sorted(failed))}) for PGUSER={role!r}; run "
            "`claude-code-telegrammer health` under the generated agent "
            "environment, provision the reported credential or repair the "
            "turn bridge, then retry `sac agents start`"
        )


def _poller_env(
    config: AgentConfig, *, home: Path, entry: dict[str, Any]
) -> dict[str, str]:
    env = os.environ.copy()
    dotenv = home / ".env"
    if dotenv.is_file():
        env.update(_read_env(dotenv))
    from ._board_identity_env import raw_args_env
    from ._fleet_env import effective_env

    env.update({str(key): str(value) for key, value in effective_env(config).items()})
    env.update(
        raw_args_env(getattr(getattr(config, "apptainer", None), "raw_args", None))
    )
    declared = entry.get("env")
    for key, raw in declared.items() if isinstance(declared, dict) else ():
        value = str(raw)
        match = _PLACEHOLDER.fullmatch(value)
        if match:
            value = env.get(match.group(1), "")
        env[str(key)] = value
    token = str(env.get("CCT_BOT_TOKEN", "") or "").strip()
    if not token:
        raise HermesCctPollerError("Hermes CCT poller has no resolved CCT_BOT_TOKEN")
    port = resolved_a2a_port(config)
    if port is None:
        raise HermesCctPollerError(
            "Hermes CCT poller requires a resolved spec.a2a.port"
        )
    turn_url = f"http://127.0.0.1:{port}/v1/turn"
    env["CCT_TURN_URL"] = turn_url
    env["CLAUDE_CODE_TELEGRAMMER_TURN_URL"] = turn_url
    # The MCP server receives the same contract through wire_hermes_cct_rail.
    # Keep it here as well so health/preflight and the authoritative child
    # describe the ownership topology identically.
    env["CLAUDE_CODE_TELEGRAMMER_EXTERNAL_POLLER"] = "1"
    env["CCT_AGENT_ID"] = config.name
    env["SAC_NAME"] = config.name
    env["SCITEX_AGENT_CONTAINER_NAME"] = config.name
    env["CCT_AGENT_STATE_DIR"] = str(
        Path.home() / ".scitex" / "claude-code-telegrammer" / "runtime" / config.name
    )
    return env


def _pid_path(config: AgentConfig, state_dir: Path | None = None) -> Path:
    return (state_dir or state_dir_for_config(config)) / PID_FILENAME


def _owns_poller(pid: int, *, name: str, proc_root: Path = Path("/proc")) -> bool:
    try:
        cmdline = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
        environ = (proc_root / str(pid) / "environ").read_bytes().split(b"\0")
    except OSError:
        return False
    command = " ".join(part.decode(errors="replace") for part in cmdline if part)
    env = dict(
        part.decode(errors="replace").split("=", 1)
        for part in environ
        if part and b"=" in part
    )
    return _POLLER_NAME in command and env.get("SAC_NAME") == name


def _owned_poller_pids(
    name: str, *, proc_root: Path = Path("/proc")
) -> tuple[int, ...]:
    """Return every live standalone poller proven to belong to ``name``."""
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return ()
    found = []
    for entry in entries:
        if entry.name.isdigit():
            pid = int(entry.name)
            if _owns_poller(pid, name=name, proc_root=proc_root):
                found.append(pid)
    return tuple(sorted(found))


def stop_cct_poller(
    config: AgentConfig,
    *,
    state_dir: Path | None = None,
    kill: Callable[[int, int], None] = os.kill,
    owns: Callable[..., bool] = _owns_poller,
    poller_pids: Callable[[str], tuple[int, ...]] = _owned_poller_pids,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Stop only the identity-proven poller recorded for this Hermes agent."""
    path = _pid_path(config, state_dir)
    try:
        recorded = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        recorded = -1
    candidates = set(poller_pids(config.name))
    if recorded > 0 and owns(recorded, name=config.name):
        candidates.add(recorded)
    elif recorded > 0:
        log.warning(
            "Hermes CCT PID %s is not owned by %s; not signalling",
            recorded,
            config.name,
        )
    signalled: set[int] = set()
    for pid in candidates:
        try:
            kill(pid, signal.SIGTERM)
            signalled.add(pid)
        except ProcessLookupError:
            pass
    deadline = monotonic() + _STOP_GRACE_S
    while monotonic() < deadline:
        live = {pid for pid in signalled if owns(pid, name=config.name)}
        if not live:
            path.unlink(missing_ok=True)
            return bool(signalled)
        sleep(0.05)
    for pid in signalled:
        if owns(pid, name=config.name):
            try:
                kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    path.unlink(missing_ok=True)
    return bool(signalled)


def start_cct_poller(
    config: AgentConfig,
    *,
    spawn: Callable[..., Any] = subprocess.Popen,
    state_dir: Path | None = None,
    stop: Callable[..., bool] = stop_cct_poller,
    preflight: Callable[[dict[str, Any], dict[str, str]], None] = _preflight_cct,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Start SAC's one managed Hermes poller, or no-op when CCT is unselected."""
    if not cct_requested(config):
        return None
    state = state_dir or state_dir_for_config(config)
    home = state / "home"
    entry = _mcp_entry(home)
    argv = _poller_argv(entry)
    env = _poller_env(config, home=home, entry=entry)
    preflight(entry, env)
    state.mkdir(parents=True, exist_ok=True)
    # Idempotent singleton boundary: retire SAC's previous owned process before
    # creating its successor. CCT's token pidfile independently resolves a race
    # with an MCP-spawned predecessor using its newest-wins takeover protocol.
    stop(config, state_dir=state)
    log_handle = open(state / LOG_FILENAME, "ab")
    try:
        process = spawn(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=env,
            start_new_session=True,
        )
    except Exception:
        log_handle.close()
        raise
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        log_handle.close()
        raise HermesCctPollerError("CCT poller spawn returned no positive PID")
    path = _pid_path(config, state)
    path.write_text(f"{pid}\n", encoding="utf-8")
    path.chmod(0o600)
    sleep(_START_SETTLE_S)
    poll = getattr(process, "poll", None)
    if callable(poll) and poll() is not None:
        path.unlink(missing_ok=True)
        log_handle.close()
        raise HermesCctPollerError(
            f"CCT poller exited during startup; inspect {state / LOG_FILENAME}"
        )
    log_handle.close()
    log.info("Hermes CCT poller started for %s (pid=%s)", config.name, pid)
    return pid


__all__ = [
    "HermesCctPollerError",
    "LOG_FILENAME",
    "PID_FILENAME",
    "start_cct_poller",
    "stop_cct_poller",
]
