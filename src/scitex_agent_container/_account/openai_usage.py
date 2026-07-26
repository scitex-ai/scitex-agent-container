"""OpenAI spend tracking — SPEND-based, not quota-based.

Sibling of :mod:`_account.claude_usage` for OpenAI-backed agents
(scitex-todo card ``openai-compat-2``). Anthropic Pro/Max accounts are
flat-rate, so the Claude tracker reports *quota utilization* (percent of
a rolling window). OpenAI API usage is pay-per-token with no quota
window — the meaningful number is **dollars spent**, tracked two ways:

1. **Local ledger** (works with any project API key, no admin scope):
   :func:`record_usage` accumulates each turn's token counts into
   ``~/.scitex/cache/openai_spend.json`` (daily buckets), pricing them
   via :func:`estimate_cost_usd`; :func:`read_spend` summarizes. The
   ``openai_session`` runner records every turn automatically.
2. **Authoritative org spend** (requires an *admin* API key):
   :func:`fetch_usage` queries ``GET
   https://api.openai.com/v1/organization/costs`` and returns billed
   USD over 1d/7d/30d windows.

Design rules (mirrors ``claude_usage.py``)
-------------------------------------------
1. API keys are read from the environment only inside this module and
   are **never** returned to callers. Only spend metrics leave.
2. :func:`fetch_usage` results are cached in
   ``~/.scitex/cache/openai_usage.json`` for 5 minutes.
3. The public functions **never raise** — failures come back as a dict
   with ``error`` set (ledger writers degrade to no-ops).
4. Pure stdlib + ``urllib.request``. No requests/httpx/openai import.
5. A key-leak guard rejects any result that smells like secret material
   before it is cached or returned.

Env contract: ``SAC_OPENAI_ADMIN_KEY`` (preferred, sac-tracked) →
``OPENAI_ADMIN_KEY``. The Costs API requires an org **admin** key — a
plain project ``OPENAI_API_KEY`` gets 401, so it is deliberately not
consulted here (the local ledger is the no-admin-scope path).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_CACHE_TTL_SECONDS = 300  # 5 minutes — matches claude_usage
_COSTS_URL = "https://api.openai.com/v1/organization/costs"
_ADMIN_KEY_ENVS = ("SAC_OPENAI_ADMIN_KEY", "OPENAI_ADMIN_KEY")
_MAX_COST_PAGES = 8  # 31-bucket pages; 8 pages ≫ the 30d window we ask for

# Result-shape sentinel — every fetch_usage return carries exactly these
# keys so downstream consumers can destructure without KeyError.
_EMPTY_RESULT: dict[str, Any] = {
    "spend_usd_1d": None,
    "spend_usd_7d": None,
    "spend_usd_30d": None,
    "currency": "usd",
    "fetched_at": None,
    "from_cache": False,
    "error": None,
}

# ---------------------------------------------------------------------------
# Price table — USD per 1M tokens, (input, output). ESTIMATES for the
# local ledger only (official list prices checked 2026-07-26; authoritative
# spend is fetch_usage's Costs API). Family-aware longest-prefix matching
# lets dated snapshots
# ("gpt-5-mini-2026-01-01") price as their family. Unknown models record
# tokens with spend contribution 0 and bump ``unpriced_turns``.
# ---------------------------------------------------------------------------
_PRICES_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-5.4-pro": (30.00, 180.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.2-pro": (21.00, 168.00),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5-pro": (15.00, 120.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "o4-mini": (1.10, 4.40),
    "o3": (2.00, 8.00),
}

_FORBIDDEN_KEY_SUBSTRINGS: tuple[str, ...] = (
    "sk-",
    "api_key",
    "apikey",
    "admin_key",
    "bearer",
    "password",
    "credential",
    "secret",
)
_FORBIDDEN_VALUE_SUBSTRINGS: tuple[str, ...] = (
    "sk-",
    "bearer ",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now_utc().isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON object from ``path``; ``None`` on any error."""
    # stx-allow: fallback (reason: cache/ledger file may not exist or may be corrupt; None signals caller to start fresh)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> bool:
    """Atomic best-effort JSON write (tmp + rename). Never raises."""
    # stx-allow: fallback (reason: cache dir may be read-only or disk-full; spend accounting is best-effort and callers are unaffected)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.rename(path)
        return True
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return False


def _cache_path(home: Path) -> Path:
    return home / ".scitex" / "cache" / "openai_usage.json"


def _ledger_path(home: Path) -> Path:
    return home / ".scitex" / "cache" / "openai_spend.json"


def _check_no_key_leak(result: dict[str, Any]) -> None:
    """Raise RuntimeError if any key/value looks like an API key or secret."""
    for key, value in result.items():
        key_l = key.lower()
        for needle in _FORBIDDEN_KEY_SUBSTRINGS:
            if needle in key_l:
                raise RuntimeError(f"openai_usage: forbidden key detected: {key!r}")
        if value is None or isinstance(value, bool):
            continue
        val_l = str(value).lower()
        for needle in _FORBIDDEN_VALUE_SUBSTRINGS:
            if needle in val_l:
                raise RuntimeError(f"openai_usage: forbidden value under key {key!r}")


# ---------------------------------------------------------------------------
# Local spend ledger — per-turn estimates, no admin key required
# ---------------------------------------------------------------------------


def estimate_cost_usd(usage: dict[str, Any], model: str) -> float | None:
    """Estimate one turn's cost in USD from its token counts.

    ``usage`` is the RunResult usage dict (``input_tokens`` /
    ``output_tokens``); ``model`` matches the price table by longest
    prefix. Returns ``None`` when the model is unknown or the counts are
    missing/non-numeric — callers record the tokens unpriced rather than
    inventing a number.
    """
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    match = ""
    for prefix in _PRICES_PER_1M:
        family_match = model == prefix or model.startswith(f"{prefix}-")
        if family_match and len(prefix) > len(match):
            match = prefix
    if not match:
        return None
    in_price, out_price = _PRICES_PER_1M[match]
    cost = (input_tokens * in_price + output_tokens * out_price) / 1_000_000
    return round(cost, 6)


def _blank_bucket() -> dict[str, Any]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "spend_usd": 0.0,
        "unpriced_turns": 0,
    }


def _accumulate(
    bucket: dict[str, Any], usage: dict[str, Any], cost: float | None
) -> None:
    bucket["requests"] += int(usage.get("requests") or 1)
    bucket["input_tokens"] += int(usage.get("input_tokens") or 0)
    bucket["output_tokens"] += int(usage.get("output_tokens") or 0)
    if cost is None:
        bucket["unpriced_turns"] += 1
    else:
        bucket["spend_usd"] = round(bucket["spend_usd"] + cost, 6)


def record_usage(
    usage: dict[str, Any],
    *,
    model: str,
    agent: str = "",
    home: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Accumulate one turn's usage into the daily spend ledger.

    Buckets by UTC date under ``days`` and keeps a running ``total``;
    ``agent`` additionally buckets under ``agents`` so multi-agent hosts
    can attribute spend. Best-effort: any failure returns ``{}`` and the
    turn proceeds (the runner must never die on accounting).

    Returns the updated day bucket (post-accumulation).
    """
    # stx-allow: fallback (reason: spend accounting must never fail the caller's turn; ledger is documented best-effort)
    try:
        _home = Path(home) if home is not None else Path.home()
        _now = now if now is not None else _now_utc()
        day = _now.date().isoformat()
        cost = estimate_cost_usd(usage or {}, model)

        path = _ledger_path(_home)
        ledger = _load_json(path) or {}
        days = ledger.setdefault("days", {})
        day_bucket = days.setdefault(day, _blank_bucket())
        total = ledger.setdefault("total", _blank_bucket())
        _accumulate(day_bucket, usage or {}, cost)
        _accumulate(total, usage or {}, cost)
        if agent:
            agents = ledger.setdefault("agents", {})
            agent_bucket = agents.setdefault(agent, _blank_bucket())
            _accumulate(agent_bucket, usage or {}, cost)
        ledger["updated_at"] = _iso_now()
        _write_json_atomic(path, ledger)
        return dict(day_bucket)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return {}


def read_spend(home: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Summarize the local spend ledger. Never raises.

    Returns ``{"spend_usd_today", "spend_usd_7d", "spend_usd_total",
    "days_tracked", "error"}`` — estimate-based (see the price-table
    caveat); the authoritative billed number is :func:`fetch_usage`.
    """
    out: dict[str, Any] = {
        "spend_usd_today": 0.0,
        "spend_usd_7d": 0.0,
        "spend_usd_total": 0.0,
        "days_tracked": 0,
        "error": None,
    }
    # stx-allow: fallback (reason: ledger may be absent/corrupt; zeros + error message beat an exception for a metrics reader)
    try:
        _home = Path(home) if home is not None else Path.home()
        _now = now if now is not None else _now_utc()
        ledger = _load_json(_ledger_path(_home))
        if ledger is None:
            out["error"] = "no spend ledger recorded yet"
            return out
        days = ledger.get("days")
        days = days if isinstance(days, dict) else {}
        today = _now.date()
        week_floor = today - timedelta(days=6)
        for day_str, bucket in days.items():
            if not isinstance(bucket, dict):
                continue
            spend = float(bucket.get("spend_usd") or 0.0)
            # stx-allow: fallback (reason: a hand-edited ledger may hold a malformed date key; skip it rather than zero the report)
            try:
                day = datetime.strptime(day_str, "%Y-%m-%d").date()
            except (
                ValueError
            ):  # stx-allow: fallback (reason: type coercion or format mismatch)
                continue
            out["days_tracked"] += 1
            out["spend_usd_total"] = round(out["spend_usd_total"] + spend, 6)
            if day == today:
                out["spend_usd_today"] = round(out["spend_usd_today"] + spend, 6)
            if week_floor <= day <= today:
                out["spend_usd_7d"] = round(out["spend_usd_7d"] + spend, 6)
        return out
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        out["error"] = f"ledger read failed: {exc}"
        return out


def read_agent_spend(agent: str, home: Path | None = None) -> dict[str, Any]:
    """Return one agent's cumulative OpenAI list-price estimate.

    This reads the local ledger only; it is not an invoice. A stable
    zero-valued shape plus ``error`` is returned when no bucket exists.
    """
    out: dict[str, Any] = {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_usd": 0.0,
        "unpriced_turns": 0,
        "error": None,
    }
    try:
        _home = Path(home) if home is not None else Path.home()
        ledger = _load_json(_ledger_path(_home))
        agents = ledger.get("agents") if isinstance(ledger, dict) else None
        bucket = agents.get(agent) if isinstance(agents, dict) else None
        if not isinstance(bucket, dict):
            out["error"] = f"no OpenAI spend recorded for agent {agent!r}"
            return out
        out["requests"] = int(bucket.get("requests") or 0)
        out["input_tokens"] = int(bucket.get("input_tokens") or 0)
        out["output_tokens"] = int(bucket.get("output_tokens") or 0)
        out["estimated_cost_usd"] = round(float(bucket.get("spend_usd") or 0.0), 6)
        out["unpriced_turns"] = int(bucket.get("unpriced_turns") or 0)
        return out
    except Exception as exc:  # stx-allow: fallback (reason: usage display must not fail on a corrupt or unreadable best-effort ledger)
        out["error"] = f"agent spend read failed: {exc}"
        return out


# ---------------------------------------------------------------------------
# Authoritative org spend — the Costs API (admin key required)
# ---------------------------------------------------------------------------


def _read_admin_key() -> str | None:
    """Resolve the admin key from env (never returned to callers)."""
    for env in _ADMIN_KEY_ENVS:
        value = os.environ.get(env, "").strip()
        if value:
            return value
    return None


def _fetch_cost_page(
    admin_key: str, params: dict[str, Any], *, opener=None
) -> dict[str, Any] | None:
    """GET one Costs API page; parsed dict or ``None`` on non-HTTP failure."""
    url = f"{_COSTS_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {admin_key}"},
        method="GET",
    )
    _opener = opener if opener is not None else urllib.request.urlopen
    # stx-allow: fallback (reason: network timeout/DNS failure hitting api.openai.com; None tells caller spend is unavailable)
    try:
        with _opener(req, timeout=15) as resp:
            raw = resp.read()
        payload = json.loads(raw)
    except urllib.error.HTTPError:  # stx-allow: fallback (reason: expected failure — surfaced to caller with status code)
        raise
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None
    return payload if isinstance(payload, dict) else None


def _bucket_amounts(payload: dict[str, Any]) -> list[tuple[int, float]]:
    """Extract ``(bucket_start_ts, usd)`` pairs from one Costs API page."""
    out: list[tuple[int, float]] = []
    for bucket in payload.get("data") or []:
        if not isinstance(bucket, dict):
            continue
        start = bucket.get("start_time")
        if not isinstance(start, (int, float)):
            continue
        usd = 0.0
        for result in bucket.get("results") or []:
            amount = result.get("amount") if isinstance(result, dict) else None
            value = amount.get("value") if isinstance(amount, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usd += float(value)
        out.append((int(start), usd))
    return out


def fetch_usage(home: Path | None = None, *, opener=None) -> dict[str, Any]:
    """Return authoritative OpenAI org spend over 1d/7d/30d windows.

    Queries the Costs API with the admin key from
    ``SAC_OPENAI_ADMIN_KEY`` / ``OPENAI_ADMIN_KEY`` (never returned),
    caches for 5 minutes at ``~/.scitex/cache/openai_usage.json``, and
    never raises — failures return the sentinel shape with ``error``
    set. Mirrors :func:`_account.claude_usage.fetch_usage` structurally;
    the metrics are DOLLARS (spend-based), not utilization percent.
    """
    _home = Path(home) if home is not None else Path.home()

    def _err(msg: str) -> dict[str, Any]:
        r = dict(_EMPTY_RESULT)
        r["fetched_at"] = _iso_now()
        r["error"] = msg
        return r

    # --- cache check ---------------------------------------------------
    cached = _load_json(_cache_path(_home))
    if cached is not None:
        fetched_at_str = cached.get("fetched_at")
        # stx-allow: fallback (reason: cache may hold a malformed timestamp; fall through to a fresh fetch)
        try:
            fetched_at = datetime.fromisoformat(fetched_at_str)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            fresh = (_now_utc() - fetched_at).total_seconds() < _CACHE_TTL_SECONDS
        except (
            TypeError,
            ValueError,
        ):  # stx-allow: fallback (reason: type coercion or format mismatch)
            fresh = False
        if fresh:
            cached["from_cache"] = True
            return cached

    # --- key (never returned) -------------------------------------------
    admin_key = _read_admin_key()
    if not admin_key:
        return _err(
            "No OpenAI admin key — export SAC_OPENAI_ADMIN_KEY (or "
            "OPENAI_ADMIN_KEY). The Costs API needs an org admin key; a "
            "project OPENAI_API_KEY is not sufficient (use read_spend() "
            "for the local estimate ledger instead)."
        )

    # --- paged fetch (30d of daily buckets) ------------------------------
    now_ts = int(time.time())
    params: dict[str, Any] = {"start_time": now_ts - 30 * 86_400, "limit": 31}
    buckets: list[tuple[int, float]] = []
    # stx-allow: fallback (reason: HTTP errors from the Costs API are surfaced as an error dict, never an exception)
    try:
        for _ in range(_MAX_COST_PAGES):
            payload = _fetch_cost_page(admin_key, params, opener=opener)
            if payload is None:
                return _err("Failed to fetch or parse Costs API response")
            buckets.extend(_bucket_amounts(payload))
            next_page = payload.get("next_page")
            if not payload.get("has_more") or not next_page:
                break
            params = dict(params, page=next_page)
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        return _err(f"HTTP {exc.code} from Costs API (admin key required)")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return _err(f"Network error: {exc}")

    # --- windowed sums ----------------------------------------------------
    result = dict(_EMPTY_RESULT)
    for window_key, seconds in (
        ("spend_usd_1d", 86_400),
        ("spend_usd_7d", 7 * 86_400),
        ("spend_usd_30d", 30 * 86_400),
    ):
        floor = now_ts - seconds
        result[window_key] = round(
            sum(usd for start, usd in buckets if start >= floor), 6
        )
    result["fetched_at"] = _iso_now()
    result["from_cache"] = False
    result["error"] = None

    # Security guard — must run before the cache write.
    try:
        _check_no_key_leak(result)
    except (
        RuntimeError
    ) as exc:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
        return _err(str(exc))

    _write_json_atomic(_cache_path(_home), result)
    return result
