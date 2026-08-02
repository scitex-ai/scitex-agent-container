"""Shape ONE agent-list row.

Extracted from :mod:`._agent_list` (512-line cap split), mirroring the
existing ``_agent_list_account`` / ``_agent_list_auth`` / ``_agent_list_remote``
siblings. One responsibility: take the already-resolved per-agent facts and
return the row dict every consumer of ``sac agents list`` reads — the human
table, ``--json``, and the fleet payload.

Resolution stays in the caller on purpose. ``_agent_list`` computes the
account label there because the suite rebinds ``_al._safe_account_for`` /
``_al._runtime_account_for`` as test seams, and a resolver called from THIS
module would ignore that rebinding — measured 2026-08-02, 85 helper tests
failed when the account precedence was moved out of ``_agent_list``.
"""

from __future__ import annotations

__all__ = ["_MOVEMENT_DEFAULTS", "_movement_fields", "build_agent_row"]

# The always-present movement trio in its empty shape — the tolerant fallback
# AND what a PERF-deferred (hidden, non-running) row carries in place of the
# movement IO. One definition so the two paths can never drift.
_MOVEMENT_DEFAULTS: dict = {
    "session_jsonl_bytes": 0,
    "session_jsonl_last_write": "",
    "heartbeat_at": "",
}


def _movement_fields(name: str) -> dict:
    """Return the three movement keys for ``name`` (always all-present).

    Operator mandate (lead a2a 1781e82a, 2026-06-14): the fleet view's
    per-agent rows must carry the same ``session_jsonl_bytes`` /
    ``session_jsonl_last_write`` / ``heartbeat_at`` trio that the
    per-agent ``agent_status`` payload exposes, so a single fleet
    ``--json`` read answers "is each agent producing?".

    Tolerant: any state-dir resolution / IO failure degrades to the
    all-defaults shape (zero bytes + empty ISO strings) so the list
    command never crashes on a movement-probe hiccup.
    """
    # stx-allow: fallback (reason: list output must never crash on a
    # state-dir probe hiccup; explicit empty-values shape is the right UX.)
    try:
        from ..._lifecycle._session_movement import (
            resolve_state_dir,
            status_movement_fields,
        )

        return status_movement_fields(resolve_state_dir(name))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return dict(_MOVEMENT_DEFAULTS)


def build_agent_row(
    *,
    name: str,
    status_val: str,
    screen_name: str,
    multiplexer: str,
    started,
    host_label: str,
    host_display: str,
    spec_path: str,
    a2a_port,
    account_label: str,
    deferred: bool,
    errors,
    liveness_unknown: bool,
    labels,
) -> dict:
    """Build one row dict.

    ``deferred`` is the DEFAULT-view perf hint: a row that will be discarded
    before rendering skips the per-row session-movement IO and gets the empty
    movement shape instead. The three movement keys are ALWAYS present either
    way (operator mandate, lead a2a 1781e82a) so a JSON consumer never has to
    test for their existence.

    The optional keys (``validation_errors`` / ``liveness_unknown`` /
    ``labels``) are attached only when they carry something, keeping an
    ordinary row free of empty noise.
    """
    row: dict = {
        "name": name,
        "status": status_val,
        "screen": screen_name,
        "multiplexer": multiplexer,
        "started_at": started,
        "host": host_label,
        "host_display": host_display,
        "path": spec_path,
        "a2a_port": a2a_port,
        "account": account_label,
    }
    row.update(dict(_MOVEMENT_DEFAULTS) if deferred else _movement_fields(name))
    if errors:
        row["validation_errors"] = errors
    if liveness_unknown:
        row["liveness_unknown"] = True
    if labels:
        row["labels"] = labels
    return row
