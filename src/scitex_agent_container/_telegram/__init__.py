"""sac Telegram bridge package (Phase 1 scaffolding).

Empty package. Phase 2 will port the bridge implementation from
``/home/ywatanabe/proj/scitex-orochi/src/scitex_orochi/_telegram_bridge.py``
to sit on sac's per-agent SSE inbox bus instead of an Orochi channel.

See ``docs/design/telegram-fold.md`` for the full plan.
"""

from __future__ import annotations

from ._bridge import TelegramBridge

__all__ = ["TelegramBridge"]
