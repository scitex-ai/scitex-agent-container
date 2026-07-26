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
from pathlib import Path

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
