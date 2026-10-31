#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Durable route-specific quota incident + admission for turn drivers.

Card ``sac-codex-python-sdk-harness-20260814`` (quota-stop proxy slice).

A structured ``usageLimitExceeded`` is CLASSIFICATION, not admission: the
turn pump records it and the driver keeps feeding the backend. This module
closes that gap with a tiny durable contract, one versioned JSON file per
state dir holding one record PER ROUTE::

    {"version": 1, "routes": {"codex-sdk": {...incident...}}}

* :func:`record_quota_incident` — appends ordered evidence the first time a
  quota-exhausted turn fails (later turns extend the evidence, never fork
  the incident). Runs BEFORE the failed turn resolves. A fresh incident
  after a clear gets a NEW stable identity and resets notification.
* :func:`quota_admits` — False while a live (uncleared) incident exists
  for the route. Drivers consult it BEFORE creating a backend turn so no
  fresh same-route request burns while exhausted. FAILS CLOSED: a missing
  file admits, but a corrupt/unreadable file or unknown version denies.
* :func:`notify_quota_incident_once` — one notification per incident via
  an injected callable returning an accepted result (the diary writer's
  row id). A falsy return means NOT accepted: the record stays pending
  and the next turn retries. The diary is the NOTIFICATION, never the
  admission state.
* :func:`clear_quota_incident` — evidence-ordered recovery: the clearing
  evidence must carry a recognized kind (``operator-clear`` or
  ``probe-success``) AND a timestamp newer than the incident's latest
  evidence. Anything else is refused and the incident stays live.

What this is NOT: rate limits, context exhaustion, session budgets and
prose banners are TRANSIENT — :func:`classify_quota_exceeded` answers
False for all of them (see the negative controls), so they never latch
anything. Held (not dropped, not completed) envelopes are the driver's
job; this module only answers admission and owns the incident record.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

STATE_VERSION = 1
INCIDENT_FILENAME = "quota_incident.json"
QUOTA_EXHAUSTED = "quota-exhausted"
USAGE_LIMIT_EXCEEDED = "usageLimitExceeded"

#: Recognized recovery-evidence kinds. A clear naming any other kind is
#: refused — recovery must be explicit, not merely a later timestamp.
RECOVERY_KINDS = ("operator-clear", "probe-success")


class QuotaBlockedError(RuntimeError):
    """A queued turn cannot run: its route has a live quota incident."""


def classify_quota_exceeded(*, harness: str, codex_error_info: Any) -> bool:
    """True only for a structured native quota-exhaustion signal.

    ``usageLimitExceeded`` on the codex harness and nothing else:
    ``rateLimitExceeded``, ``contextWindowExceeded``,
    ``sessionBudgetExceeded`` and prose banners all answer False and must
    never latch a quota incident.
    """
    return harness == "codex-sdk" and codex_error_info == USAGE_LIMIT_EXCEEDED


def _incident_path(state_dir: Path) -> Path:
    return Path(state_dir) / INCIDENT_FILENAME


def _valid_at(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _route_is_cleared(record: Any) -> bool:
    """Strict recovery proof: a dict whose cleared flag is boolean True.

    Anything else present (null, wrong type, truthy non-bool like the
    string "false", dict without the flag) proves nothing and is treated
    as live/malformed downstream — never as recovery. Shared by the
    admission gate and the daemon listing so pump and daemon agree.
    """
    return isinstance(record, dict) and record.get("cleared") is True


def _read_store(state_dir: Path) -> tuple[dict, bool]:
    """Return (routes, corrupt). Missing file is NOT corrupt (admits)."""
    try:
        raw = json.loads(_incident_path(state_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return {}, True
    routes = raw.get("routes")
    if not isinstance(routes, dict):
        return {}, True
    return routes, False


def _write_store(state_dir: Path, routes: dict) -> None:
    """Atomically replace the store (temp file + rename, same directory).

    Readers therefore observe the old or the new document, never a torn
    partial write; a torn read can only fail closed, never admit.
    """
    path = _incident_path(state_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"version": STATE_VERSION, "routes": routes}), encoding="utf-8"
    )
    os.replace(tmp, path)


@contextmanager
def _store_locked(state_dir: Path):
    """Exclusive cross-process guard for read-modify-write cycles.

    Two recorders racing (driver turn + direct pump call) must not
    interleave read/add/write and lose one route's evidence. Readers
    take no lock: atomic replace keeps them torn-free.
    """
    path = Path(state_dir) / (INCIDENT_FILENAME + ".lock")
    with open(path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_quota_incident(
    state_dir: Path,
    *,
    route: str,
    detail: str,
    at: float | None = None,
) -> dict | None:
    """Append quota-exhausted evidence; create the incident on first sight.

    A new incident after a clear gets a fresh stable identity
    (``route:first_seen``) and resets notification, so the next cap
    alerts again. Non-finite timestamps fall back to now (documented;
    only finite times order evidence). Returns None WITHOUT writing
    when the existing store is corrupt/unreadable: the unreadable state
    is left untouched (never silently replaced, so no other route is
    unblocked) and admission stays failed-closed.
    """
    now = at if _valid_at(at) else time.time()
    with _store_locked(state_dir):
        routes, corrupt = _read_store(state_dir)
        if corrupt:
            return None
        record = routes.get(route)
        if (
            isinstance(record, dict)
            and record.get("cleared") is not True
            and isinstance(record.get("evidence"), list)
        ):
            live_record = record
        else:
            # Recovered before (fresh identity), or malformed (replace —
            # a malformed record already denies, so replacing it with a
            # well-formed live one changes no verdict for this route and
            # touches no other route).
            live_record = {
                "id": f"{route}:{now}",
                "route": route,
                "cause": QUOTA_EXHAUSTED,
                "evidence": [],
                "notified": False,
                "cleared": False,
            }
            routes[route] = live_record
        live_record["evidence"].append({"detail": detail, "at": now})
        live_record["cleared"] = False
        _write_store(state_dir, routes)
        return live_record


def quota_admits(state_dir: Path, route: str) -> bool:
    """False while ``route`` has a live incident; fail closed on corruption.

    A missing file admits (nothing latched). A PRESENT route record that
    is malformed (not a dict, or a dict without an explicit cleared flag)
    denies: fail closed, never admit on a shape that cannot prove recovery.
    """
    routes, corrupt = _read_store(state_dir)
    if corrupt:
        return False
    if route not in routes:
        return True
    return _route_is_cleared(routes[route])


def blocked_routes(state_dir: Path) -> list:
    """Routes with a live incident; empty when clear. Corrupt reads all.

    Uses the same strict-cleared classification as the admission gate,
    so a malformed present record blocks the daemon exactly where the
    pump denies: the two can never disagree.
    """
    routes, corrupt = _read_store(state_dir)
    if corrupt:
        return ["*"]
    return [
        route
        for route, record in routes.items()
        if not _route_is_cleared(record)
    ]


def notify_quota_incident_once(
    state_dir: Path,
    *,
    route: str,
    notify: Callable[[dict], Any],
) -> bool:
    """Notify once per incident; True only on an accepted result.

    ``notify`` receives the incident record and returns its accepted
    result (e.g. the diary row id). A falsy return means NOT accepted:
    the record stays pending (``notified`` False) and the next turn
    retries. superflous calls after an accepted notification are no-ops
    returning False.
    """
    with _store_locked(state_dir):
        routes, corrupt = _read_store(state_dir)
        if corrupt:
            return False
        record = routes.get(route)
        if not isinstance(record, dict) or record.get("notified") is True:
            return False
        if not notify(record):
            return False
        record["notified"] = True
        _write_store(state_dir, routes)
        return True


def clear_quota_incident(
    state_dir: Path,
    *,
    route: str,
    evidence: dict | None = None,
    evidence_at: float | None = None,
) -> bool:
    """Clear the incident on verified recovery evidence, else refuse.

    ``evidence`` must be ``{"at": <finite>, "kind": <recognized>}`` with
    ``at`` NEWER than the incident's latest evidence. Stale, undated,
    unrecognized-kind and wrong-route clears all return False with the
    incident live. ``evidence_at=<float>`` is shorthand for a successful
    recovery probe at that time (``{"at": evidence_at,
    "kind": "probe-success"}``).
    """
    if evidence is None:
        if evidence_at is None:
            return False
        evidence = {"at": evidence_at, "kind": "probe-success"}
    with _store_locked(state_dir):
        routes, corrupt = _read_store(state_dir)
        if corrupt:
            return False
        record = routes.get(route)
        if not isinstance(record, dict):
            return False
        at = evidence.get("at") if isinstance(evidence, dict) else None
        kind = evidence.get("kind") if isinstance(evidence, dict) else None
        if kind not in RECOVERY_KINDS or not _valid_at(at):
            return False
        assert isinstance(at, (int, float))
        seen = [e.get("at", 0) for e in record.get("evidence", [])]
        latest = max(seen) if seen else 0
        if at <= latest:
            return False
        record["cleared"] = True
        record["cleared_at"] = at
        record["cleared_kind"] = kind
        _write_store(state_dir, routes)
        return True


__all__ = [
    "INCIDENT_FILENAME",
    "QUOTA_EXHAUSTED",
    "QuotaBlockedError",
    "RECOVERY_KINDS",
    "STATE_VERSION",
    "USAGE_LIMIT_EXCEEDED",
    "blocked_routes",
    "classify_quota_exceeded",
    "clear_quota_incident",
    "notify_quota_incident_once",
    "quota_admits",
    "record_quota_incident",
]
