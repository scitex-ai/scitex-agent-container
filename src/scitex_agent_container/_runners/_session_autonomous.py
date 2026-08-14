"""F-CS3 phase 2 — the autonomous drive-until-done loop.

Extracted from :mod:`.session_daemon` (line cap) when the v4 step-5
liveness artifact landed there. ``session_daemon`` (and, through it,
``claude_session``) re-export ``_autonomous_loop`` so every existing
call shape keeps resolving.
"""

from __future__ import annotations

import asyncio

__all__ = ["_autonomous_loop"]


async def _autonomous_loop(
    inbox: "asyncio.Queue",
    *,
    mission: str,
    drive_until: str,
    max_turns: int,
    kick_text: str,
    stop: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
) -> int:
    """Drive turns until ``drive_until`` matches an assistant reply or
    ``max_turns`` is reached.

    Returns 0 on a clean ``drive_until`` match, 1 if the cap is hit
    without a match. Always sets ``stop`` before returning so the
    surrounding daemon shuts down cleanly.

    F-CS3 phase 2 — pairs with the schema landed in phase 1
    (``spec.autonomous`` in agent yaml).
    """
    from ._session_inbox import TurnEnvelope

    text = mission
    rc = 1
    for _ in range(max(1, max_turns)):
        if stop.is_set():
            break
        env = TurnEnvelope(text=text, response=loop.create_future(), exit_after=False)
        await inbox.put(env)
        try:
            reply = await env.response
        except Exception:  # stx-allow: fallback (reason: convo task may fail mid-loop; treat as terminal — set stop and exit non-zero)
            break
        if drive_until and drive_until in (reply or ""):
            rc = 0
            break
        text = kick_text
    stop.set()
    return rc
