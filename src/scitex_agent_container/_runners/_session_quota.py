"""Per-agent token-quota totals persisted in the runner state dir.

Extracted from ``_session_state.py`` to keep that module under the
512-line cap. ``_session_state`` re-exports ``read_quota`` /
``accumulate_quota`` (explicit ``as`` aliases) so every existing
``_session_state.read_quota`` / ``.accumulate_quota`` importer keeps
working unchanged.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from .._usage_period import (
    parse_usage_timestamp,
    timestamp_in_period,
    usage_timestamp_iso,
)
from ._atomic import atomic_write_text


def _zero_quota() -> dict:
    """Return the stable persisted usage-counter shape."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "turns": 0,
        # Provider-reported API-equivalent cost. For subscription-backed
        # Claude Code this is NOT necessarily an amount charged to the
        # operator; it is the cost value emitted by ResultMessage.
        "cost_usd": 0.0,
        "costed_turns": 0,
        "uncosted_turns": 0,
    }


def _quota_path(state_dir: Path) -> Path:
    return state_dir / "quota.json"


def read_quota(state_dir: Path | None) -> dict:
    """Return persisted usage totals with a backward-compatible shape."""
    if state_dir is None:
        return _zero_quota()
    p = _quota_path(state_dir)
    if not p.is_file():
        return _zero_quota()
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _zero_quota()
    if not isinstance(loaded, dict):
        return _zero_quota()
    totals = _zero_quota()
    totals.update(loaded)
    return totals


def _accumulate_result(totals: dict, record: dict) -> None:
    """Accumulate one persisted SDK result record into ``totals``."""
    usage = record.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            totals[key] += value
    totals["turns"] += 1
    cost = record.get("cost_usd")
    valid_cost = (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(float(cost))
        and float(cost) >= 0.0
    )
    if valid_cost:
        totals["cost_usd"] = round(totals["cost_usd"] + float(cost), 8)
        totals["costed_turns"] += 1
    else:
        totals["uncosted_turns"] += 1


def read_quota_period(
    state_dir: Path | None,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    """Aggregate timestamped SDK results in the half-open requested period.

    The SDK runner has always appended its result usage to ``session.jsonl``.
    That journal provides historical resolution while ``quota.json`` remains
    the fast all-time counter. Invalid or missing timestamps are excluded.
    """
    selected = _zero_quota()
    selected.update(
        {
            "first_observed_at": None,
            "last_observed_at": None,
            "retained_first_observed_at": None,
            "retained_last_observed_at": None,
            "timestamped_turns": 0,
            "untimestamped_turns": 0,
            "cumulative_turns": 0,
            "history_matches_cumulative_totals": False,
            "error": None,
        }
    )
    cumulative = read_quota(state_dir)
    selected["cumulative_turns"] = int(cumulative.get("turns", 0) or 0)
    path = state_dir / "session.jsonl" if state_dir is not None else None
    if path is None or not path.is_file():
        selected["error"] = (
            "cumulative SDK usage exists but no timestamped journal is retained"
            if selected["cumulative_turns"] > 0
            else "no timestamped SDK usage journal recorded yet"
        )
        return selected
    retained = _zero_quota()
    selected_first: datetime | None = None
    selected_last: datetime | None = None
    retained_first: datetime | None = None
    retained_last: datetime | None = None
    try:
        stream = path.open(encoding="utf-8", errors="replace")
    except OSError as exc:
        selected["error"] = f"SDK usage journal read failed: {exc}"
        return selected
    with stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("type") != "result":
                continue
            timestamp = parse_usage_timestamp(record.get("ts"))
            _accumulate_result(retained, record)
            if timestamp is None:
                selected["untimestamped_turns"] += 1
                continue
            selected["timestamped_turns"] += 1
            if retained_first is None or timestamp < retained_first:
                retained_first = timestamp
            if retained_last is None or timestamp > retained_last:
                retained_last = timestamp
            if not timestamp_in_period(timestamp, since, until):
                continue
            _accumulate_result(selected, record)
            if selected_first is None or timestamp < selected_first:
                selected_first = timestamp
            if selected_last is None or timestamp > selected_last:
                selected_last = timestamp
    comparable = tuple(_zero_quota())
    selected["history_matches_cumulative_totals"] = all(
        retained[key] == cumulative[key] for key in comparable
    )
    selected["first_observed_at"] = usage_timestamp_iso(selected_first)
    selected["last_observed_at"] = usage_timestamp_iso(selected_last)
    selected["retained_first_observed_at"] = usage_timestamp_iso(retained_first)
    selected["retained_last_observed_at"] = usage_timestamp_iso(retained_last)
    return selected


def accumulate_quota(
    state_dir: Path,
    usage: dict | None,
    *,
    cost_usd: float | int | None = None,
) -> dict:
    """Add one ``ResultMessage`` usage/cost block to running totals.

    Atomic via a per-writer-unique tmp + rename so a concurrent
    ``sac agent status`` reader never sees a partial write, and two
    writers sharing the dir never collide on the tmp name. Returns the
    new totals. ``cost_usd`` is provider-reported; no price table is
    guessed here.
    """
    if not usage and cost_usd is None:
        return read_quota(state_dir)
    totals = read_quota(state_dir)
    usage = usage or {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        totals[key] = int(totals.get(key, 0)) + int(usage.get(key, 0) or 0)
    totals["turns"] = int(totals.get("turns", 0)) + 1
    valid_cost = (
        isinstance(cost_usd, (int, float))
        and not isinstance(cost_usd, bool)
        and math.isfinite(float(cost_usd))
        and float(cost_usd) >= 0.0
    )
    if valid_cost:
        totals["cost_usd"] = round(
            float(totals.get("cost_usd", 0.0) or 0.0) + float(cost_usd),
            8,
        )
        totals["costed_turns"] = int(totals.get("costed_turns", 0)) + 1
    else:
        totals["uncosted_turns"] = int(totals.get("uncosted_turns", 0)) + 1
    atomic_write_text(_quota_path(state_dir), json.dumps(totals))
    return totals
