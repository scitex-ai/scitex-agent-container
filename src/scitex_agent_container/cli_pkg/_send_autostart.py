"""Start-on-send wiring (thin re-export of the shared doctrine).

The implementation lives in :mod:`.._reconcile._autostart` so BOTH
injection seams (this send path and the TUI turn bridge) enforce one
doctrine without a ``cli_pkg`` <-> ``runtimes`` import cycle. This module
keeps its name so existing imports and tests keep working.
"""

from __future__ import annotations

from .._reconcile._autostart import maybe_autostart_for_send

__all__ = ["maybe_autostart_for_send"]
