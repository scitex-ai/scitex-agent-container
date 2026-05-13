"""Parser for ``spec.telegram``."""

from __future__ import annotations

from .._types import TelegramSpec


def parse_telegram(spec: dict) -> TelegramSpec:
    raw = spec.get("telegram", {}) or {}
    return TelegramSpec(
        bot_token_env=raw.get(
            "bot_token_env", "SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN"
        ),
        allowed_users=[str(u) for u in (raw.get("allowed_users", []) or [])],
        auto_connect=raw.get("auto_connect", True),
        greeting=raw.get("greeting", ""),
    )
