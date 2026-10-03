"""Allowlisted account-usage projections; absent measurements stay unknown.

Codex quota follows the read-only wham primary/secondary window shape.
CommandCode1.58 billing credits are USD; its window resetAt is milliseconds
(compared to Date.now by the official CLI). Go exposes percent/resetsAt.
No price table, model entitlement or generation-admission inference belongs here.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

CACHE_SECONDS = 300


def number(value):
    """Accept finite, nonnegative provider measurements, never booleans."""
    return (
        float(value)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def timestamp(value, *, milliseconds=False):
    """Normalize an aware ISO string or numeric epoch to UTC."""
    try:
        if type(value) in (int, float) and math.isfinite(value):
            date = datetime.fromtimestamp(value / (1000 if milliseconds else 1), timezone.utc)
        elif isinstance(value, str):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                return None
        else:
            return None
        return date.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def snapshot_state(fetched_at, now):
    """Old snapshots are stale; absent, invalid or future times are unknown."""
    stamp = timestamp(fetched_at)
    if stamp is None:
        return "unknown"
    age = (now - datetime.fromisoformat(stamp)).total_seconds()
    if age < 0:
        return "unknown"
    return "known" if age <= CACHE_SECONDS else "stale"


def window(name, percent, reset, now, *, duration=None, milliseconds=False):
    """Keep expiration and malformed reset evidence distinct from current quota."""
    used = number(percent)
    used = used if used is not None and used <= 100 else None
    reset_at = timestamp(reset, milliseconds=milliseconds)
    state = "unknown"
    if used is not None and reset_at:
        state = "known" if datetime.fromisoformat(reset_at) > now else "stale"
    return {
        "name": name, "unit": "percent", "used": used,
        "remaining": 100 - used if used is not None else None,
        "resetAt": reset_at, "duration_seconds": number(duration), "state": state,
    }


def project(provider, payloads, now):
    """Return metric fields only; raw bodies and account/org IDs never escape."""
    out = {
        "fetchedAt": now.isoformat(), "windows": [], "balances": {},
        "cost": None, "subscription_fee": None, "currency": None,
    }
    if provider == "openai":
        data = payloads.get("quota") or {}
        rate = data.get("rate_limit") if isinstance(data, dict) else None
        for name in ("primary_window", "secondary_window"):
            item = rate.get(name) if isinstance(rate, dict) else None
            if isinstance(item, dict):
                out["windows"].append(window(
                    name, item.get("used_percent"), item.get("reset_at"), now,
                    duration=item.get("limit_window_seconds"),
                ))
    elif provider == "opencode-go":
        data = payloads.get("quota") or {}
        usage = data.get("usage") if isinstance(data, dict) else None
        for name in ("rolling", "weekly", "monthly"):
            item = usage.get(name) if isinstance(usage, dict) else None
            item = item if isinstance(item, dict) else {}
            out["windows"].append(window(name, item.get("percent"), item.get("resetsAt"), now))
    elif provider == "commandcode":
        data = payloads.get("credits") or {}
        credits = data.get("credits") if isinstance(data, dict) else None
        if isinstance(credits, dict):
            out["currency"] = "USD"
            out["balances"] = {
                name: number(credits.get(field))
                for name, field in (
                    ("monthly_included", "monthlyCredits"),
                    ("purchased", "purchasedCredits"), ("free", "freeCredits"),
                )
            }
        limits = data.get("windowLimits") if isinstance(data, dict) else None
        for name in ("fiveHour", "weekly"):
            item = limits.get(name) if isinstance(limits, dict) else None
            item = item if isinstance(item, dict) else {}
            used, cap = number(item.get("used")), number(item.get("cap"))
            percent = min(100, used / cap * 100) if used is not None and cap else None
            out["windows"].append(window(
                name, percent, item.get("resetAt"), now, milliseconds=True,
            ))
        summary = payloads.get("cost") or {}
        spent = number(summary.get("totalCost")) if isinstance(summary, dict) else None
        if spent is not None:
            out["currency"] = "USD"
            out["cost"] = {
                "amount": spent, "currency": "USD",
                "window_start": payloads.get("cost_since"), "window_end": now.isoformat(),
            }
    measured = (
        any(row["state"] == "known" for row in out["windows"])
        or any(value is not None for value in out["balances"].values())
        or out["cost"] is not None
    )
    out["usage_state"] = "known" if measured else "unknown"
    return out


def safe_snapshot(raw, now):
    """Revalidate normalized cache bytes rather than trust a copied envelope."""
    if not isinstance(raw, dict):
        return None
    stamp = timestamp(raw.get("fetchedAt"))
    if stamp is None:
        return None
    out = {
        "fetchedAt": stamp, "usage_state": snapshot_state(stamp, now),
        "windows": [], "balances": {}, "cost": None,
        "subscription_fee": None, "currency": None,
    }
    windows = raw.get("windows")
    for item in windows if isinstance(windows, list) else []:
        if not isinstance(item, dict) or item.get("name") not in {
            "primary_window", "secondary_window", "rolling", "weekly", "monthly", "fiveHour",
        }:
            continue
        row = window(item["name"], item.get("used"), item.get("resetAt"), now,
                     duration=item.get("duration_seconds"))
        if out["usage_state"] != "known" and row["state"] == "known":
            row["state"] = out["usage_state"]
        out["windows"].append(row)
    if raw.get("currency") == "USD":
        out["currency"] = "USD"
        balances = raw.get("balances")
        if isinstance(balances, dict):
            out["balances"] = {
                name: number(balances.get(name))
                for name in ("monthly_included", "purchased", "free")
                if name in balances
            }
        cost = raw.get("cost")
        if isinstance(cost, dict) and number(cost.get("amount")) is not None:
            start, end = timestamp(cost.get("window_start")), timestamp(cost.get("window_end"))
            if start and end and datetime.fromisoformat(start) <= datetime.fromisoformat(end):
                out["cost"] = {
                    "amount": number(cost["amount"]), "currency": "USD",
                    "window_start": start, "window_end": end,
                }
    measured = (
        any(row["state"] == "known" for row in out["windows"])
        or any(value is not None for value in out["balances"].values())
        or out["cost"] is not None
    )
    if not measured and out["usage_state"] == "known":
        out["usage_state"] = "stale" if any(row["state"] == "stale" for row in out["windows"]) else "unknown"
    if raw.get("usage_state") == "unknown":
        out["usage_state"] = "unknown"
    return out
