"""sac listen / MCP-server startup hook for the Telegram bridge.

The bridge is the singleton long-poller for a bot token. Per the design
doc it is owned by the lead's session — only one process per box should
boot it. This module gives the host process (sac MCP server or the lead's
``sac listen``) a single entry point :func:`maybe_start_bridge` that:

1. Returns early when ``LEAD_TELEGRAM_AUTH_TOKEN`` is unset (subagents,
   non-lead processes, or development containers without the lead env).
2. Returns early when neither the long-form nor the short-form bot-token
   env var is set.
3. Constructs a :class:`TelegramBridge` with the allowlist + target agent
   from the resolved ``TelegramSpec``.
4. Logs a WARN — the dependency on the launcher's
   ``--dangerously-load-development-channels server:scitex-agent-container``
   flag is not auto-detectable from this side; without it Claude Code will
   silently drop our channel notifications.
5. Stashes the instance in :mod:`._runtime` so the ``telegram_*`` MCP
   tools can find it without parameter threading.

The actual ``await bridge.start()`` is deferred to the caller's event
loop (because constructor work is sync and Python's ``asyncio.run`` is
not re-entrant). The MCP server's ``lifespan`` is the natural place to
``await``.

This module never raises on missing env — silence is the lead-only,
subagents-still-work UX. A WARN log is the loudest signal we ever emit
when "I should have started but didn't" might actually be a bug.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from .._env import getenv
from ._bridge import ChannelNotifier, TelegramBridge
from ._runtime import set_bridge

log = logging.getLogger("scitex_agent_container.telegram")

LEAD_AUTH_TOKEN_ENV = "LEAD_TELEGRAM_AUTH_TOKEN"


@dataclass
class _SpecLike:
    """Shape that :func:`maybe_start_bridge` reads.

    We accept anything that exposes these attributes — usually a
    :class:`config._types.TelegramSpec`, but tests pass a plain dataclass
    here too.
    """

    bot_token_env: str
    allowed_users: list[str]
    auto_connect: bool


def _coerce_spec(spec: Any) -> Optional[_SpecLike]:
    if spec is None:
        return None
    try:
        return _SpecLike(
            bot_token_env=getattr(
                spec, "bot_token_env", "SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN"
            ),
            allowed_users=list(getattr(spec, "allowed_users", []) or []),
            auto_connect=bool(getattr(spec, "auto_connect", True)),
        )
    except Exception as exc:  # pragma: no cover  # stx-allow: fallback (reason: malformed spec object should not crash boot)
        log.warning("telegram: malformed spec object: %s", exc)
        return None


def maybe_start_bridge(
    spec: Any = None,
    *,
    notifier: Optional[ChannelNotifier] = None,
    target_agent: str = "master",
) -> Optional[TelegramBridge]:
    """Construct + register a bridge if the env conditions allow.

    Returns the constructed bridge (NOT started — caller awaits
    ``bridge.start()``) on success, or None if the bridge should not run
    in this process.

    The bridge instance is also stashed in :mod:`._runtime` so the MCP
    tools can find it without the host plumbing through a handle.
    """
    coerced = _coerce_spec(spec) or _SpecLike(
        bot_token_env="SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN",
        allowed_users=[],
        auto_connect=True,
    )

    if not coerced.auto_connect:
        log.info("telegram: auto_connect=false; bridge will not start")
        return None

    auth_token = os.environ.get(LEAD_AUTH_TOKEN_ENV)
    if not auth_token:
        log.info(
            "telegram: %s unset — this process is not the lead, bridge "
            "will not start (subagent / non-lead container)",
            LEAD_AUTH_TOKEN_ENV,
        )
        return None

    # Resolve the bot token. Honour both prefixed forms by stripping the
    # known prefix on the configured env var if present.
    bot_token: Optional[str] = os.environ.get(coerced.bot_token_env)
    if not bot_token:
        # Fall back to the sac dual-prefix lookup
        if coerced.bot_token_env.startswith("SAC_"):
            bot_token = getenv(coerced.bot_token_env[len("SAC_") :])
        elif coerced.bot_token_env.startswith("SCITEX_AGENT_CONTAINER_"):
            bot_token = getenv(coerced.bot_token_env[len("SCITEX_AGENT_CONTAINER_") :])
    if not bot_token:
        log.warning("telegram: %s unset; cannot start bridge", coerced.bot_token_env)
        return None

    # WARN: launcher dependency on --dangerously-load-development-channels
    log.warning(
        "telegram: bridge starting. NOTE: inbound channel notifications "
        "require the Claude Code launcher to be invoked with "
        "`--dangerously-load-development-channels "
        "server:scitex-agent-container`. Without it, Claude will drop "
        "notifications/claude/channel emissions silently."
    )

    bridge = TelegramBridge(
        bot_token=bot_token,
        allowed_users=coerced.allowed_users,
        target_agent=target_agent,
        notifier=notifier,
    )
    set_bridge(bridge, auth_token=auth_token)
    return bridge


def mint_bridge_auth_token() -> str:
    """Create a per-session auth token when one wasn't pre-shared.

    Reserved for future use — the current contract reads the token from
    ``LEAD_TELEGRAM_AUTH_TOKEN`` env, which the launcher sets from the
    lead's ``~/.scitex/lead/.env``. Kept here so the API stays stable
    when we add Phase 4 dynamic token rotation.
    """
    return "sac-telegram-" + secrets.token_hex(16)


__all__ = [
    "LEAD_AUTH_TOKEN_ENV",
    "maybe_start_bridge",
    "mint_bridge_auth_token",
]
