"""Additive local quota/fee surface; historical account blocks stay intact."""

from __future__ import annotations

from ._terminal_text import terminal_safe


def provider_usage(openai_accounts, *, passive=False, refresh=False, budget=4.0):
    """Resolve configured names once and collect usage without credential renewal."""
    from .._account.provider_usage_inventory import collect, discover

    return collect(discover(openai_accounts), passive=passive, refresh=refresh, budget=budget)


def render_provider_usage(rows):
    """Show exact provider metrics, their age, and explicit unknown fees/costs."""
    if not rows:
        return ""
    lines = ["Local provider usage and fees (usage-only; no credential renewal)"]
    for row in rows:
        state = row.get("usage_state", "unknown")
        lines.append(f"- {terminal_safe(row['qualified_id'])}: {'current' if state == 'known' else state}")
        lines.append("  fetchedAt: " + (row.get("fetchedAt") or "unknown"))
        if len(row["aliases"]) > 1:
            lines.append("  Shared credential aliases: " + ", ".join(terminal_safe(x) for x in row["aliases"]))
        for item in row.get("windows", []):
            used = item.get("used")
            percent = "unknown" if used is None else f"{used:g}% used"
            metric_state = state if state != "known" else item["state"]
            lines.append(f"  {item['name']}: {percent}; reset {item.get('resetAt') or 'unknown'} ({metric_state})")
        for name, amount in row.get("balances", {}).items():
            lines.append(f"  {name} balance: " + ("unknown" if amount is None else f"USD {amount:g}"))
        cost = row.get("cost")
        lines.append(
            f"  Measured cost: USD {cost['amount']:g} [{cost['window_start']} / {cost['window_end']}]"
            if cost else "  Measured cost: unknown"
        )
        lines.append("  Subscription fee: unknown")
        lines.append("  Selection: explicit; automatic rotation: not implemented")
        if row.get("error"):
            lines.append("  Metadata status: " + row["error"])
    # Accounts may share organization billing. Deliberately no fleet cost total.
    return "\n".join(lines)
