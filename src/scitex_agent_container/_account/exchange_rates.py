"""Best-effort USD/JPY reference-rate resolution with a local cache."""

from __future__ import annotations

import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_CACHE_TTL_SECONDS = 86_400


def _result() -> dict[str, Any]:
    return {
        "rate": None,
        "rate_date": None,
        "source": None,
        "from_cache": False,
        "stale": False,
        "error": None,
    }


def _valid_rate(value: Any) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if 0.0 < rate < 1_000.0 else None


def _cache_path(home: Path) -> Path:
    return home / ".scitex" / "cache" / "usd_jpy_rate.json"


def _read_cache(home: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and _valid_rate(data.get("rate")) else None


def _write_cache(home: Path, data: dict[str, Any]) -> None:
    try:
        path = _cache_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _cache_fresh(data: dict[str, Any], now: datetime) -> bool:
    try:
        fetched = datetime.fromisoformat(str(data["fetched_at"]))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return (now - fetched).total_seconds() < _CACHE_TTL_SECONDS


def _fetch_ecb(opener=None) -> tuple[float, str]:
    request = urllib.request.Request(
        ECB_DAILY_URL,
        headers={"User-Agent": "scitex-agent-container/usage"},
    )
    open_url = opener if opener is not None else urllib.request.urlopen
    with open_url(request, timeout=5) as response:
        root = ET.fromstring(response.read())
    usd = jpy = None
    rate_date = ""
    for node in root.iter():
        if "time" in node.attrib:
            rate_date = node.attrib["time"]
        currency = node.attrib.get("currency")
        if currency == "USD":
            usd = _valid_rate(node.attrib.get("rate"))
        elif currency == "JPY":
            jpy = _valid_rate(node.attrib.get("rate"))
    if usd is None or jpy is None or not rate_date:
        raise ValueError("ECB response did not contain USD, JPY, and date")
    return jpy / usd, rate_date


def resolve_usd_jpy_rate(
    *,
    home: Path | None = None,
    override: float | None = None,
    opener=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve JPY per USD: override → env → fresh ECB cache → ECB fetch."""
    out = _result()
    explicit = override
    if explicit is None:
        env_value = os.environ.get("SAC_USD_JPY_RATE")
        explicit = _valid_rate(env_value) if env_value else None
    rate = _valid_rate(explicit)
    if rate is not None:
        out.update(rate=rate, source="override", rate_date=None)
        return out
    host_home = Path(home) if home is not None else Path.home()
    current = now if now is not None else datetime.now(timezone.utc)
    cached = _read_cache(host_home)
    if cached is not None and _cache_fresh(cached, current):
        out.update(
            rate=float(cached["rate"]),
            rate_date=cached.get("rate_date"),
            source=ECB_DAILY_URL,
            from_cache=True,
        )
        return out
    try:
        rate, rate_date = _fetch_ecb(opener)
    except Exception as exc:  # stx-allow: fallback (reason: FX display is optional; stale cache or explicit unavailable beats failing usage inspection)
        if cached is not None:
            out.update(
                rate=float(cached["rate"]),
                rate_date=cached.get("rate_date"),
                source=ECB_DAILY_URL,
                from_cache=True,
                stale=True,
                error=f"ECB refresh failed: {exc}",
            )
        else:
            out["error"] = f"USD/JPY unavailable: {exc}"
        return out
    payload = {
        "rate": round(rate, 8),
        "rate_date": rate_date,
        "fetched_at": current.isoformat(),
    }
    _write_cache(host_home, payload)
    out.update(
        rate=payload["rate"],
        rate_date=rate_date,
        source=ECB_DAILY_URL,
    )
    return out


__all__ = ["ECB_DAILY_URL", "resolve_usd_jpy_rate"]
