"""Orochi auto-connect sidecar -- registers agent with Orochi hub on startup."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AgentConfig

log = logging.getLogger("agent-container.orochi")


def start_orochi_sidecar(config: AgentConfig) -> threading.Thread | None:
    """Start Orochi connection in a background daemon thread.

    Returns the thread (for testing), or None if Orochi is not enabled.
    """
    if not config.orochi.is_enabled:
        return None

    # Resolve token from env var
    token = os.environ.get(config.orochi.token_env, "")
    if not token:
        # Also check agent's own env dict (set via YAML)
        token = config.env.get(config.orochi.token_env, "")
    if not token:
        log.warning(
            "Orochi token env var '%s' not set -- skipping auto-connect. "
            "Fix: export %s=<your-token>",
            config.orochi.token_env,
            config.orochi.token_env,
        )
        return None

    thread = threading.Thread(
        target=_run_connector,
        args=(config, token),
        name=f"orochi-{config.name}",
        daemon=True,
    )
    thread.start()
    log.info(
        "Orochi sidecar started for '%s' -> hosts=%s port=%d",
        config.name,
        config.orochi.hosts,
        config.orochi.port,
    )
    return thread


def _run_connector(config: AgentConfig, token: str) -> None:
    """Run the async Orochi connection loop in a new event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_connect_loop(config, token))
    except Exception:
        log.error("Orochi connector crashed", exc_info=True)
    finally:
        loop.close()


async def _try_host(
    OrochiClient: type,
    host: str,
    config: AgentConfig,
    token: str,
    machine: str,
    role: str,
    channels: list[str],
) -> object | None:
    """Try connecting to a single host. Returns client on success, None on failure."""
    orochi = config.orochi
    try:
        client = OrochiClient(
            name=config.name,
            host=host,
            port=orochi.port,
            channels=channels,
            token=token,
            machine=machine,
            role=role,
            agent_id=f"{config.name}@{machine}",
            ws_path=orochi.ws_path,
        )
        await asyncio.wait_for(client.connect(), timeout=10)
        return client
    except Exception as exc:
        log.warning("Orochi host %s:%d FAILED: %s", host, orochi.port, exc)
        return None


async def _connect_loop(config: AgentConfig, token: str) -> None:
    """Connect to Orochi with multi-host fallback and retry logic."""
    try:
        from scitex_orochi._client import OrochiClient
    except ImportError:
        log.error(
            "scitex-orochi not installed -- cannot auto-connect. "
            "Fix: pip install scitex-orochi"
        )
        return

    orochi = config.orochi
    machine = config.labels.get("machine", platform.node())
    role = config.labels.get("role", "")
    channels = orochi.channels or ["#general"]
    attempt = 0

    while True:
        attempt += 1
        max_retries = orochi.reconnect_max_retries
        if max_retries > 0 and attempt > max_retries:
            log.error(
                "Orochi connection failed after %d attempts -- giving up",
                max_retries,
            )
            return

        # Try each host in order — report every result
        client = None
        results: list[str] = []
        connected_host = None
        for host in orochi.hosts:
            client = await _try_host(
                OrochiClient, host, config, token, machine, role, channels
            )
            if client is not None:
                results.append(f"{host}:OK")
                connected_host = host
                break
            else:
                results.append(f"{host}:FAIL")

        # Always report connection status (no silent fallback)
        status_line = " | ".join(results)
        if connected_host:
            log.info(
                "Orochi connection report: [%s] -- connected via %s "
                "(%s@%s channels=%s)",
                status_line,
                connected_host,
                config.name,
                machine,
                channels,
            )
        else:
            log.error(
                "Orochi connection report: [%s] -- ALL HOSTS FAILED (attempt %d)",
                status_line,
                attempt,
            )

        if client is not None:
            try:
                # Heartbeat (standalone only, Django ignores unknown types)
                try:
                    await client.start_heartbeat(interval=orochi.heartbeat_interval)
                except Exception:
                    log.debug("Heartbeat not supported by server, skipping")

                # Status update (standalone only)
                try:
                    await client.update_status(status="online", current_task="ready")
                except Exception:
                    log.debug("Status update not supported by server, skipping")

                # Listen for messages (keeps connection alive)
                async for msg in client.listen():
                    log.debug(
                        "Orochi msg [%s] %s: %s",
                        msg.payload.get("channel", "?"),
                        msg.sender,
                        msg.payload.get("content", "")[:80],
                    )
            except Exception:
                log.warning(
                    "Orochi connection lost (attempt %d)", attempt, exc_info=True
                )
        # (failure already reported above in connection report)

        # Retry
        log.info("Reconnecting to Orochi in %ds...", orochi.reconnect_interval)
        await asyncio.sleep(orochi.reconnect_interval)
