"""A2A turn-bridge seam mixin for the TUI runtime.

Extracted from :mod:`tui_session` to keep that module under the line limit.
The turn bridge gives an interactive TUI agent the same ``/v1/turn`` endpoint
the SDK runner serves, so a bus-pushed message WAKES the idle TUI. Both hooks
are best-effort: a bridge spawn/teardown failure must never wedge start/stop.
"""

from __future__ import annotations

import logging

from ..config import AgentConfig

__all__ = ["TurnBridgeSeamMixin"]


class TurnBridgeSeamMixin:
    """Best-effort A2A turn-bridge start/stop seams.

    Expects the host class to expose ``self._turn_bridge_start`` /
    ``self._turn_bridge_stop`` (injectable seams; ``None`` → resolve the real
    launcher lazily, avoiding the :mod:`_tui_turn_bridge` import cycle).
    """

    def _maybe_start_turn_bridge(self, config: AgentConfig) -> None:
        """Start the A2A turn bridge (best-effort; lazy default seam).

        No-op for agents without a resolved ``a2a.port`` (the launcher returns
        None). Tests inject a recording ``turn_bridge_start`` so no subprocess
        is spawned.
        """
        fn = self._turn_bridge_start
        if fn is None:
            from ._tui_turn_bridge import start_turn_bridge

            fn = start_turn_bridge
        try:
            fn(config)
        except Exception as exc:  # stx-allow: fallback (reason: a bridge spawn failure must never wedge agent start — the agent still runs, only wake-on-push is degraded; logged for the operator)
            logging.getLogger(__name__).warning(
                "TuiSessionRuntime: A2A turn bridge failed to start for %s: %s",
                getattr(config, "name", "?"),
                exc,
            )

    def _maybe_stop_turn_bridge(self, config: AgentConfig) -> None:
        """Stop the A2A turn bridge (best-effort; lazy default seam)."""
        fn = self._turn_bridge_stop
        if fn is None:
            from ._tui_turn_bridge import stop_turn_bridge

            fn = stop_turn_bridge
        try:
            fn(config)
        except Exception as exc:  # stx-allow: fallback (reason: bridge teardown is best-effort; a failure must not block stop())
            logging.getLogger(__name__).warning(
                "TuiSessionRuntime: A2A turn bridge failed to stop for %s: %s",
                getattr(config, "name", "?"),
                exc,
            )
