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

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


_active_session: Optional[Any] = None
_patched: bool = False


def get_active_session() -> Optional[Any]:
    """Return the most recently constructed ServerSession, or None.

    The returned type is ``Any`` to avoid forcing callers to import
    ``mcp.server.session.ServerSession`` at module load — the bridge
    uses duck typing on ``send_message``.
    """
    return _active_session


def install() -> bool:
    """Idempotently patch ``ServerSession.__init__`` to register every
    instance in the module-level holder.

    Returns ``True`` if the patch was installed (or already installed),
    ``False`` if the MCP library couldn't be imported (degrade silently
    so the MCP server still boots).
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
        log.debug("session-holder: captured ServerSession %s", id(self))

    ServerSession.__init__ = _capturing_init  # type: ignore[method-assign]
    _patched = True
    log.debug("session-holder: ServerSession.__init__ patched")
    return True


def _reset_for_tests() -> None:
    """Clear the active-session holder. Test-only — not part of the
    public API."""
    global _active_session
    _active_session = None


__all__ = ["get_active_session", "install"]
