"""OpenAI/Codex account formatting for ``sac accounts list``."""

from __future__ import annotations

from ._timefmt import format_jst


def format_openai_account_block(meta: dict) -> list[str]:
    """Render display-safe Codex login metadata as human-readable lines."""
    if not meta or not any(value is not None for value in meta.values()):
        return []

    def _fmt(value: object) -> str:
        return "-" if value is None else str(value)

    auth_mode = meta.get("auth_mode")
    mode_label = {
        "chatgpt": "ChatGPT",
        "apikey": "API key",
        "api_key": "API key",
    }.get(auth_mode, _fmt(auth_mode))
    organization = _fmt(meta.get("organization_name"))
    role = meta.get("organization_role")
    if role and organization != "-":
        organization += f" ({role})"

    lines = [
        "OpenAI Codex account",
        f"  Email:          {_fmt(meta.get('email_address'))}",
        f"  Organization:   {organization}",
        f"  Display name:   {_fmt(meta.get('display_name'))}",
        f"  Auth mode:      {mode_label}",
        f"  Plan:           {_fmt(meta.get('plan_type'))}",
        f"  Account ID:     {_fmt(meta.get('account_id'))}",
        f"  Since:          {format_jst(meta.get('subscription_active_start'))}",
        f"  Until:          {format_jst(meta.get('subscription_active_until'))}",
        f"  Last refresh:   {format_jst(meta.get('last_refresh'))}",
    ]
    alias = meta.get("gateway_alias")
    if alias:
        lines.insert(1, f"  Gateway alias:  {alias}")
    return lines


__all__ = ["format_openai_account_block"]
