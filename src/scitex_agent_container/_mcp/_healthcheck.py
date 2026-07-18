"""Boot self-check + auto-heal for the critical MCP connections.

Fleet incident 2026-07-06 (belt-and-suspenders layer). The deterministic fix
for the cold-start connect race lives in the distributed config
(``alwaysLoad:true`` + a raised ``MCP_TIMEOUT`` — see
:mod:`scitex_agent_container.runtimes._mcp_reliability`). THIS module is the
recovery net for the residual cases those cannot cover: a critical MCP server
that dies AFTER connecting, or one that is genuinely broken (won't connect at
all) rather than merely slow.

Run at agent boot (a ``SessionStart`` hook invokes ``sac mcp healthcheck``):

  1. LOG the expected capability surface. If a critical MCP is down, an agent
     that reaches for ``host_exec``/``agent_spawn``/``db_*`` or the todo tools
     gets a "tool not found" — which reads as "I lack that capability" unless
     the boot log makes explicit that those tools come from an MCP that FAILED
     to connect. The log turns a silent capability gap into a diagnosable
     "MCP broken → heal".
  2. DETECT which critical servers connected, via ``claude mcp list`` (Claude
     Code's live per-server connectivity check).
  3. HEAL a detected failure: raise a loud, operator-visible alarm and — rate
     limited, to avoid a boot⇄restart loop — request a self-restart through the
     host ``sac listen`` control plane (the same broker ``sac agents restart``
     uses). A ``--fresh`` restart re-runs the whole launch, giving the (now
     ``alwaysLoad`` + long-``MCP_TIMEOUT``) connect another, blocking, attempt.

FAIL-OPEN THROUGHOUT: every step is defensive. A healthcheck that itself errors
(no ``claude`` binary, unparseable output, restart broker unreachable, …) must
NEVER block the agent's boot — it logs and returns. The worst case degrades to
today's behaviour (tools missing), never worse.

HONEST-UNKNOWN (coordinator dogfood 2026-07-09): when connectivity cannot be
verified — ``claude mcp list`` is unreadable/empty, or a critical server is absent
from its output — the result is ``action="unknown"``, NEVER ``"ok"``. A live
server PROCESS does not prove the stdio CLIENT is still connected: a mid-session
load-spike drop leaves the process up but the tools gone, and Claude Code does not
reconnect a stdio MCP mid-session (only a full session restart revives it). A
false-OK would mask exactly that drop, so it is categorically refused — a false-OK
health check is worse than none. See ``docs/mcp-load-resilience.md``.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

# Critical MCP servers → the capability surface each one gates. These are
# LOOKUP KEYS into the ``.mcp.json`` the fleet deploys — a file sac does NOT
# emit (it comes from the operator's to_home layers). Logged verbatim at boot.
CRITICAL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "scitex-agent-container": (
        "host_exec_local (run host commands)",
        "agent_spawn / agent_start / agent_restart (manage peers)",
        "db_query / db_show (state DB)",
        "host_exec / host_list (multi-host)",
    ),
    "scitex-cards": (
        "add_task / update_task / complete_task",
        "list_tasks / comment_task (the fleet task board)",
    ),
}

# Transitional server-key aliases (package renamed scitex-todo → scitex-cards,
# 2026-07-16). We report under the PREFERRED key above, but a live fleet is
# rolled one agent at a time and the ``.mcp.json`` is not ours to flip, so an
# agent may still declare the server under its OLD key. Accepting both is not a
# nicety here — it is required for correctness. A hard flip would classify every
# not-yet-migrated agent's healthy board MCP as absent, and absent means UNKNOWN,
# which alarms. That is a false alarm manufactured by the rename itself.
# Drop the old entry once no deployed ``.mcp.json`` declares it.
SERVER_ALIASES: dict[str, tuple[str, ...]] = {
    "scitex-cards": ("scitex-todo",),
}

# Statuses parsed out of ``claude mcp list``.
CONNECTED = "connected"
FAILED = "failed"
UNKNOWN = "unknown"

# Self-restart cooldown: never self-restart for a failed MCP more than once per
# this window, so a genuinely-broken server can't drive a tight boot⇄restart
# loop (the alarm still fires every boot, so a human sees a persistent break).
_RESTART_COOLDOWN_S = 1800.0

# Env knobs (all optional; defaults are safe).
ENV_DISABLE = "SAC_MCP_HEALTHCHECK_DISABLED"
ENV_NO_RESTART = "SAC_MCP_HEALTHCHECK_NO_RESTART"
ENV_STATE_DIR = "SAC_MCP_HEALTHCHECK_STATE_DIR"


def _state_dir() -> Path:
    """Directory for the restart-cooldown sentinel (override via env for tests)."""
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "mcp-health"


def _default_mcp_list_runner() -> str:
    """Run ``claude mcp list`` and return combined stdout+stderr (best-effort).

    Returns ``""`` on any failure (missing binary, timeout, …) — the parser then
    yields ``unknown`` for every server and the check degrades to a no-op.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:  # stx-allow: fallback (reason: fail-open — a broken/absent `claude` binary must never block boot)
        log.warning("mcp healthcheck: `claude mcp list` failed to run (%s)", exc)
        return ""
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def parse_mcp_status(
    text: str,
    servers: Iterable[str],
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, str]:
    """Classify each ``server`` as connected / failed / unknown from ``claude
    mcp list`` output.

    ``claude mcp list`` prints one line per server, e.g.::

        scitex-agent-container: sac mcp start  - ✓ Connected
        scitex-cards: scitex-cards mcp start  - ✗ Failed to connect

    We match the server name at the start of a line (``name:`` prefix) and read
    a connected/failed marker from that line. Robust to unicode-glyph vs plain
    "Connected"/"Failed" wording. Any server not mentioned → ``unknown``.

    ``aliases`` (default :data:`SERVER_ALIASES`) maps a requested name to OTHER
    server keys that may carry the same server during a rename roll-out. A line
    matching any alias resolves to the REQUESTED key, so the caller always reads
    one canonical name regardless of which spelling the agent's ``.mcp.json``
    used. Only aliases of names actually requested are consulted, so a caller
    asking for a legacy name verbatim still gets exactly that name back.
    """
    alias_map = SERVER_ALIASES if aliases is None else aliases
    result: dict[str, str] = {name: UNKNOWN for name in servers}
    # requested name → every spelling that counts as that server.
    candidates: dict[str, tuple[str, ...]] = {
        name: (name, *alias_map.get(name, ())) for name in result
    }
    if not text:
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        for name in result:
            # Match "<name>:" or "<name> " at the line start (mcp list uses
            # "name: command - status"). Substring fallback covers reformats.
            if any(
                line.startswith(f"{alias}:")
                or line.startswith(f"{alias} ")
                or alias in line
                for alias in candidates[name]
            ):
                if (
                    "fail" in low
                    or "✗" in line
                    or "not connect" in low
                    or "error" in low
                ):
                    result[name] = FAILED
                elif "connect" in low or "✓" in line or "ready" in low:
                    # "failed to connect" already handled above; here it's the
                    # positive "Connected".
                    result[name] = CONNECTED
                break
    return result


def log_capability_surface(statuses: dict[str, str]) -> None:
    """Log the expected capability surface, flagging any down critical server.

    So a later "tool not found" is diagnosable as "MCP broken → heal", not
    misread as "I lack that capability".
    """
    for server, caps in CRITICAL_CAPABILITIES.items():
        status = statuses.get(server, UNKNOWN)
        surface = "; ".join(caps)
        if status == CONNECTED:
            log.info("mcp healthcheck: '%s' CONNECTED — provides: %s", server, surface)
        elif status == FAILED:
            log.warning(
                "mcp healthcheck: '%s' FAILED TO CONNECT — the following tools "
                "are UNAVAILABLE this session (this is an MCP connect failure, "
                "NOT a missing capability): %s",
                server,
                surface,
            )
        else:
            log.info(
                "mcp healthcheck: '%s' status UNKNOWN (could not read "
                "`claude mcp list`) — expected tools: %s",
                server,
                surface,
            )


def _recent_restart(now: float, state_dir: Path) -> bool:
    """True when a self-restart was requested within the cooldown window."""
    sentinel = state_dir / "last-restart.ts"
    try:
        prev = float(sentinel.read_text().strip())
    except Exception:  # stx-allow: fallback (no/again unreadable sentinel → treat as "no recent restart")
        return False
    return (now - prev) < _RESTART_COOLDOWN_S


def _record_restart(now: float, state_dir: Path) -> None:
    """Stamp the cooldown sentinel (best-effort)."""
    sentinel = state_dir / "last-restart.ts"
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(f"{now}\n")
    except OSError as exc:  # stx-allow: fallback (can't write sentinel → skip cooldown, still fail-open)
        log.warning("mcp healthcheck: could not record restart sentinel (%s)", exc)


def _env_flag(name: str) -> bool:
    """True when env var ``name`` is set to a truthy string."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _default_self_restart(agent_name: str) -> bool:
    """Broker a ``--fresh`` self-restart via the host ``sac listen`` plane.

    Returns True when the restart request was accepted. Fail-open: any error
    (missing ``SAC_LISTEN_BASE_URL``, ACL denial, transport failure) logs and
    returns False — the agent keeps running, degraded, rather than crashing.
    """
    try:
        from .._lifecycle._restart_client import RestartRequestError, request_restart
    except Exception as exc:  # stx-allow: fallback (lifecycle client unavailable → cannot self-heal, stay up)
        log.warning("mcp healthcheck: restart client unavailable (%s)", exc)
        return False
    try:
        request_restart(agent_name, caller=agent_name, fresh=True)
    except RestartRequestError as exc:
        log.warning(
            "mcp healthcheck: self-restart request for '%s' was rejected (%s)",
            agent_name,
            exc,
        )
        return False
    except Exception as exc:  # stx-allow: fallback (any transport/env error → stay up, don't crash boot)
        log.warning("mcp healthcheck: self-restart request errored (%s)", exc)
        return False
    log.warning(
        "mcp healthcheck: requested a --fresh self-restart of '%s' to re-attempt "
        "the failed MCP connect(s)",
        agent_name,
    )
    return True


def run_healthcheck(
    *,
    mcp_list_runner: Callable[[], str] | None = None,
    restart_fn: Callable[[str], bool] | None = None,
    agent_name: str | None = None,
    now_fn: Callable[[], float] = time.time,
    state_dir: Path | None = None,
    disabled: bool | None = None,
    allow_restart: bool | None = None,
) -> dict:
    """Boot self-check + auto-heal. FAIL-OPEN — never raises.

    Returns a result dict::

        {"statuses": {server: status}, "failed": [server, ...],
         "healed": bool, "action": "ok"|"restart-requested"|"alarm-only"|
                                    "restart-cooldown"|"disabled"|"error"}

    Injection seams (real callables / values, no mocks): ``mcp_list_runner``
    supplies the ``claude mcp list`` output; ``restart_fn`` performs the
    self-restart and returns whether it was accepted; ``state_dir`` locates the
    cooldown sentinel; ``disabled`` / ``allow_restart`` override the env knobs.
    Production leaves them ``None`` — the module then shells out / brokers to
    the host plane and reads :data:`ENV_DISABLE` / :data:`ENV_NO_RESTART` /
    :data:`ENV_STATE_DIR` from the environment.
    """
    try:
        is_disabled = disabled if disabled is not None else _env_flag(ENV_DISABLE)
        if is_disabled:
            log.info("mcp healthcheck: disabled")
            return {"statuses": {}, "failed": [], "healed": False, "action": "disabled"}

        resolved_state_dir = state_dir if state_dir is not None else _state_dir()
        runner = mcp_list_runner or _default_mcp_list_runner
        servers = tuple(CRITICAL_CAPABILITIES.keys())
        text = runner()
        statuses = parse_mcp_status(text, servers)
        log_capability_surface(statuses)

        failed = [s for s, st in statuses.items() if st == FAILED]
        unknown = [s for s, st in statuses.items() if st == UNKNOWN]
        if not failed:
            if unknown:
                # HONEST-UNKNOWN (coordinator dogfood 2026-07-09): we could NOT
                # read client-side connectivity — ``claude mcp list`` was
                # unreadable/empty, or a critical server was absent from its
                # output. A live server PROCESS does not prove the stdio CLIENT is
                # still connected: a mid-session load-spike drop leaves the process
                # up but the tools gone (Claude Code does not reconnect a stdio MCP
                # mid-session). So a "no FAILED lines" reading must NEVER be
                # reported as OK — that false-OK masks exactly the drop we care
                # about. A false-OK health check is worse than none: report
                # UNKNOWN and alarm.
                log.warning(
                    "mcp healthcheck: could NOT verify MCP connectivity for %s "
                    "(`claude mcp list` unreadable/empty) — reporting UNKNOWN, NOT "
                    "ok. A live server process does not prove the stdio client is "
                    "still connected; a dropped stdio MCP is only revived by a full "
                    "session restart.",
                    ", ".join(unknown),
                )
                return {
                    "statuses": statuses,
                    "failed": [],
                    "unknown": unknown,
                    "healed": False,
                    "action": "unknown",
                }
            return {
                "statuses": statuses,
                "failed": [],
                "unknown": [],
                "healed": False,
                "action": "ok",
            }

        # A critical MCP FAILED to connect — alarm LOUD (operator-visible).
        log.warning(
            "mcp healthcheck: CRITICAL MCP connection failure — %s did not "
            "connect. Tools from those servers are unavailable this session.",
            ", ".join(failed),
        )

        name = (
            agent_name
            or os.environ.get("SAC_NAME")
            or os.environ.get("SCITEX_AGENT_CONTAINER_NAME", "")
        )
        restart_allowed = (
            allow_restart
            if allow_restart is not None
            else not _env_flag(ENV_NO_RESTART)
        )
        if not restart_allowed or not name:
            return {
                "statuses": statuses,
                "failed": failed,
                "healed": False,
                "action": "alarm-only",
            }

        now = now_fn()
        if _recent_restart(now, resolved_state_dir):
            log.warning(
                "mcp healthcheck: a self-restart was already requested within "
                "the cooldown; NOT restarting again (alarm stands). Servers "
                "still failing: %s",
                ", ".join(failed),
            )
            return {
                "statuses": statuses,
                "failed": failed,
                "healed": False,
                "action": "restart-cooldown",
            }

        restart = restart_fn or _default_self_restart
        accepted = bool(restart(name))
        if accepted:
            _record_restart(now, resolved_state_dir)
        return {
            "statuses": statuses,
            "failed": failed,
            "healed": accepted,
            "action": "restart-requested" if accepted else "alarm-only",
        }
    except Exception as exc:  # stx-allow: fallback (the heal-check itself must NEVER block or crash the agent boot)
        log.warning("mcp healthcheck: self-check errored, ignoring (%s)", exc)
        return {"statuses": {}, "failed": [], "healed": False, "action": "error"}


__all__ = [
    "CONNECTED",
    "CRITICAL_CAPABILITIES",
    "FAILED",
    "SERVER_ALIASES",
    "UNKNOWN",
    "log_capability_surface",
    "parse_mcp_status",
    "run_healthcheck",
]
