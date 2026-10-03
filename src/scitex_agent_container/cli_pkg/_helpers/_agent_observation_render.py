"""Incremental human detail from the same safe JSON observation."""

from __future__ import annotations

import json

from .._terminal_text import terminal_safe


def work_cell(row):
    observation = row.get("observation") or {}
    return terminal_safe(observation.get("work") or "unknown")


def detail_lines(row, level):
    observation = row.get("observation") or {}
    lines = [
        f"{terminal_safe(row.get('name') or '?')} observed:",
        f"  Process: {terminal_safe(observation.get('process') or 'unknown')}; work: {work_cell(row)}",
    ]
    activity = observation.get("activity") or {}
    if activity.get("counters"):
        lines.append(
            "  Owned counters: "
            + terminal_safe(json.dumps(activity["counters"], sort_keys=True))
        )
    if level >= 1:
        stored_credential = row.get("stored_credential") or row.get("account")
        if stored_credential:
            lines.append("  Stored credential: " + terminal_safe(stored_credential))
        selection = observation.get("selection") or {}
        lines.extend(
            [
                "  Selected provider: "
                + terminal_safe(selection.get("provider") or "unknown"),
                "  Account group: "
                + terminal_safe(selection.get("account_group") or "unknown")
                + " ("
                + terminal_safe(selection.get("group_scope") or "unknown")
                + ")",
                "  Plan: "
                + terminal_safe(selection.get("plan") or "unknown")
                + "; capacity: "
                + terminal_safe(
                    (observation.get("capacity") or {}).get("state") or "unknown"
                ),
            ]
        )
    if level >= 2:
        versions = observation.get("versions") or {}
        for key in ("private_environment", "image_environment", "loaded_process"):
            value = versions.get(key) or {"state": "unknown"}
            lines.append(
                "  "
                + key.replace("_", " ")
                + ": "
                + terminal_safe(json.dumps(value, sort_keys=True))
            )
        lines.append(
            "  Private/image version skew: "
            + terminal_safe(versions.get("skew") or "unknown")
        )
    if level >= 3:
        for key in ("instance_id", "evidence_source", "observed_at", "work_reason"):
            lines.append(
                "  "
                + key
                + ": "
                + terminal_safe(
                    observation.get(key)
                    if observation.get(key) is not None
                    else "unknown"
                )
            )
    return lines
