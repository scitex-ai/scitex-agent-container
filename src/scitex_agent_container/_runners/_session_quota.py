"""Per-agent token-quota totals persisted in the runner state dir.

Extracted from ``_session_state.py`` to keep that module under the
512-line cap. ``_session_state`` re-exports ``read_quota`` /
``accumulate_quota`` (explicit ``as`` aliases) so every existing
``_session_state.read_quota`` / ``.accumulate_quota`` importer keeps
working unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from ._atomic import atomic_write_text


def _quota_path(state_dir: Path) -> Path:
    return state_dir / "quota.json"


def read_quota(state_dir: Path) -> dict:
    """Return the persisted quota totals, or a zeroed dict if absent."""
    p = _quota_path(state_dir)
    if not p.is_file():
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "turns": 0,
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def accumulate_quota(state_dir: Path, usage: dict | None) -> dict:
    """Add one ``ResultMessage.usage`` block to the running totals.

    Atomic via a per-writer-unique tmp + rename so a concurrent
    ``sac agent status`` reader never sees a partial write, and two
    writers sharing the dir never collide on the tmp name. Returns the
    new totals.
    """
    if not usage:
        return read_quota(state_dir)
    totals = read_quota(state_dir)
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        totals[key] = int(totals.get(key, 0)) + int(usage.get(key, 0) or 0)
    totals["turns"] = int(totals.get("turns", 0)) + 1
    atomic_write_text(_quota_path(state_dir), json.dumps(totals))
    return totals
