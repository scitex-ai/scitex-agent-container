"""Capture the live :class:`mcp.server.session.ServerSession` for
background-task notification emission.

FastMCP only exposes the session inside request handlers via the
``mcp.shared.context.request_ctx`` ContextVar. Background tasks (like
this package's Telegram long-poller) run outside any request, so they
cannot read that ContextVar.

To let the bridge emit ``notifications/claude/channel`` we monkey-patch
``ServerSession.__init__`` once, register every constructed instance in
a module-level holder, and let the notifier look it up. The MCP stdio
transport constructs exactly one session per connection, so the holder
reliably tracks the active one.

This is a private, narrow patch — it does not change MCP behaviour, it
only observes session construction. The patch is idempotent: calling
:func:`install` more than once is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


_active_session: Optional[Any] = None
_patched: bool = False
_on_session_captured: list[Callable[[Any], None]] = []


def get_active_session() -> Optional[Any]:
    """Return the most recently constructed ServerSession, or None."""
    return _active_session


def on_session_captured(cb: Callable[[Any], None]) -> None:
    """Register a callback fired immediately after each ServerSession is
    constructed. Runs synchronously inside ``ServerSession.__init__``,
    so callbacks must be cheap (schedule async work via
    ``asyncio.create_task`` rather than awaiting).
    """
    _on_session_captured.append(cb)


def install() -> bool:
    """Idempotently patch ``ServerSession.__init__`` to register every
    instance in the module-level holder and fire registered callbacks.
    """
    global _patched
    if _patched:
        return True
    try:
        from mcp.server.session import ServerSession
    except Exception as exc:  # stx-allow: fallback (reason: mcp may be unavailable in non-MCP contexts)
        log.debug("session-holder: mcp.server.session unavailable (%s)", exc)
        return False

    _orig_init = ServerSession.__init__

    def _capturing_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        global _active_session
        _orig_init(self, *args, **kwargs)
        _active_session = self
        log.info("session-holder: captured ServerSession %s", id(self))
        for cb in list(_on_session_captured):
            try:
                cb(self)
            except Exception:  # stx-allow: fallback (reason: callback failure must not break session init)
                log.exception("session-holder: callback %s raised", cb)

    ServerSession.__init__ = _capturing_init  # type: ignore[method-assign]
    _patched = True
    log.info(
        "session-holder: ServerSession.__init__ patched (+%d callback(s))",
        len(_on_session_captured),
    )
    return True


def schedule_bridge_autostart(bridge: Any) -> None:
    """Register a callback that schedules ``bridge.start()`` the next
    time a ServerSession is constructed. Idempotent: a second call
    while the bridge is already running will be a no-op inside
    ``bridge.start()``.
    """

    def _starter(_session: Any) -> None:
        # Use get_running_loop(), not get_event_loop(): the latter is
        # deprecated and on Py3.10+ returns a fresh non-running loop when
        # called from a synchronous frame inside an async stack, which
        # makes ``loop.is_running()`` falsely False and silently skips
        # ``bridge.start()``. get_running_loop() raises if there's none,
        # which is what we want.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning(
                "session-holder: no running loop at ServerSession init; "
                "cannot schedule bridge.start()"
            )
            return
        log.info(
            "session-holder: scheduling bridge.start() via running loop %s", id(loop)
        )
        task = loop.create_task(bridge.start())

        def _on_done(t):  # type: ignore[no-untyped-def]
            # Without this hook, exceptions raised by bridge.start() vanish
            # into the void (fire-and-forget task). Log them so the
            # debug.log captures the real failure.
            if t.cancelled():
                log.warning("session-holder: bridge.start() was cancelled")
                return
            exc = t.exception()
            if exc is not None:
                log.exception("session-holder: bridge.start() raised", exc_info=exc)
            else:
                log.info("session-holder: bridge.start() completed cleanly")

        task.add_done_callback(_on_done)

    on_session_captured(_starter)


def _reset_for_tests() -> None:
    """Clear the active-session holder + callbacks. Test-only."""
    global _active_session
    _active_session = None
    _on_session_captured.clear()


__all__ = [
    "get_active_session",
    "install",
    "on_session_captured",
    "schedule_bridge_autostart",
]
