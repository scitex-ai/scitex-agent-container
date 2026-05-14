"""Snapshot-diff JSON construction.

Flat dotted-key diff between two snapshots so the dashboard can highlight
what changed. Lists compare as whole values (no index explosion). The
``timestamp`` field is always ignored.
"""

from __future__ import annotations

from typing import Any


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, key))
    elif isinstance(obj, list):
        # Lists compare as whole values — don't explode indices.
        out[prefix] = obj
    else:
        out[prefix] = obj
    return out


def compute_diff_fields(
    prev: dict[str, Any] | None, latest: dict[str, Any]
) -> list[str]:
    if prev is None:
        return []
    flat_prev = _flatten(prev)
    flat_latest = _flatten(latest)
    # Ignore timestamp — it always changes.
    ignored = {"timestamp"}
    changed: list[str] = []
    keys = set(flat_prev) | set(flat_latest)
    for k in sorted(keys):
        if k in ignored:
            continue
        if flat_prev.get(k) != flat_latest.get(k):
            changed.append(k)
    return changed
