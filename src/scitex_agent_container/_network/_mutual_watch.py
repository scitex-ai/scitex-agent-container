"""Mutual heartbeat-watch — bidirectional peer freshness probe.

Operator mandate (lead a2a 1781e82a, 2026-06-14):
"agents (and lead) cross-monitor heartbeat_at + session.jsonl growth;
a stale peer raises a STRUCTURAL alert, not a silent drift."

This module owns the pure, stateless decision: "given a peer's
state-dir snapshot (now + heartbeat.json + session.jsonl stat +
the prior reading), is the peer stale?" It does NOT touch SQLite,
spawn threads, or read the registry — those concerns live in the
caller (the heartbeat-loop integration). Keeping the decision pure
makes it trivially testable with real fixtures (no mocks).

Two staleness kinds, both addressable independently so the lead can
distinguish "the peer's whole runner has died" from "the peer is
heartbeating but produced nothing":

  * ``KIND_STALE_HEARTBEAT`` — ``heartbeat.json`` ``ts`` is older
    than ``heartbeat_threshold_s`` (default 180 s) against the
    observer's wall clock. This is the classic "the peer's runner
    process is wedged or dead" signal.
  * ``KIND_STALE_SESSION_JSONL`` — heartbeat IS fresh but
    ``session.jsonl`` has not grown in ``jsonl_idle_threshold_s``
    (default 300 s) AND ``heartbeat.state`` reports ``"working"``
    or any non-idle state. This is the "the peer SAYS it is
    working but produced zero output" signal — the silent drift
    the operator wanted surfaced.

Mutual = bidirectional: each agent runs the watch over its peers,
so A → B and B → A independently emit alerts. The "mutual" property
falls out of the symmetry, not a special pair-mode.

Threshold defaults are tunable through env vars so an operator can
ratchet them down during a debug sweep without a code change:

  * ``SAC_MUTUAL_WATCH_HEARTBEAT_STALE_S`` (default ``180``).
  * ``SAC_MUTUAL_WATCH_JSONL_IDLE_S``     (default ``300``).

:func:`load_watch_config` reads them with safe fallbacks (invalid
float → default) so a typo in the env never wedges the watch.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .._state.state_db_alerts import (
    KIND_STALE_HEARTBEAT,
    KIND_STALE_SESSION_JSONL,
)

# Defaults are conservative — short enough to catch a wedged
# runner within a few minutes, long enough to ride out a slow
# Claude turn without false-positives. Operator overrides via env.
_DEFAULT_HEARTBEAT_STALE_S = 180.0
_DEFAULT_JSONL_IDLE_S = 300.0

_ENV_HEARTBEAT_STALE = "SAC_MUTUAL_WATCH_HEARTBEAT_STALE_S"
_ENV_JSONL_IDLE = "SAC_MUTUAL_WATCH_JSONL_IDLE_S"


# Non-idle heartbeat states — when the peer claims one of these
# but session.jsonl has not moved, that is the silent-drift case
# the operator wanted surfaced. ``idle`` / ``stopping`` are
# expected to be quiet so we don't fire on them.
_NON_IDLE_STATES = frozenset({"working", "starting", "error"})


@dataclass(frozen=True)
class WatchConfig:
    """Threshold knobs for :func:`check_peer_freshness`.

    Held as a frozen dataclass so a caller can pass the same config
    object across a batch of peer checks without risk of mutation.
    Defaults reflect the conservative spec; overrides arrive via
    :func:`load_watch_config` reading the env vars.
    """

    heartbeat_threshold_s: float = _DEFAULT_HEARTBEAT_STALE_S
    jsonl_idle_threshold_s: float = _DEFAULT_JSONL_IDLE_S


@dataclass(frozen=True)
class StalePeerAlert:
    """Typed alert evidence emitted when a peer fails a freshness check.

    Schema mirrors the ``structural_alerts`` table row so the caller
    can pass these straight to :func:`state_db_alerts.record_alert`.
    JSON-serialisable so the lead can read it off ``sac db query``
    or ``sac a2a status`` without a Python import.

    Fields:

      * ``observer`` / ``peer`` — agent names (who watched / who looks
        stale).
      * ``kind`` — one of :data:`KIND_STALE_HEARTBEAT` or
        :data:`KIND_STALE_SESSION_JSONL`.
      * ``age_seconds`` — observed age (peer's last beat OR
        session.jsonl mtime) at the moment of the check.
      * ``threshold_s`` — the configured limit the age breached.
      * ``evidence`` — verbatim non-PII snapshot of the proof
        (peer_state_dir, last_heartbeat_ts, last_session_jsonl_mtime,
        last_session_jsonl_bytes, peer_state). Encoded into the
        ``evidence_json`` DB column.
    """

    observer: str
    peer: str
    kind: str
    age_seconds: float
    threshold_s: float
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Return a plain dict — what the DB writer + JSON consumers expect."""
        return {
            "observer": self.observer,
            "peer": self.peer,
            "kind": self.kind,
            "age_seconds": self.age_seconds,
            "threshold_s": self.threshold_s,
            "evidence": self.evidence,
        }


def load_watch_config() -> WatchConfig:
    """Build a :class:`WatchConfig` from env vars, falling back to defaults.

    Invalid float values (typo, empty string) degrade silently to the
    default — better the watch keeps running with a conservative
    threshold than wedge on a typo. Negative values are clamped to 0.
    """

    def _read(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            return default
        return max(0.0, value)

    return WatchConfig(
        heartbeat_threshold_s=_read(_ENV_HEARTBEAT_STALE, _DEFAULT_HEARTBEAT_STALE_S),
        jsonl_idle_threshold_s=_read(_ENV_JSONL_IDLE, _DEFAULT_JSONL_IDLE_S),
    )


def _read_peer_heartbeat(peer_state_dir: Path) -> dict | None:
    """Return the peer's ``heartbeat.json`` dict, or None when unreadable.

    Never raises — a corrupt JSON or missing file degrades to None so
    the caller treats it as "no recent heartbeat evidence" and fires
    the stale-heartbeat alert. This mirrors the
    :func:`_session_state.read_heartbeat` shape.
    """
    p = peer_state_dir / "heartbeat.json"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _peer_session_jsonl_mtime(peer_state_dir: Path) -> tuple[float | None, int | None]:
    """Return ``(mtime, size_bytes)`` for the peer's ``session.jsonl``.

    Both are ``None`` when the file is absent (peer has never written a
    turn). Reads stat-only — never opens the file — so a giant transcript
    does not slow the watch loop.
    """
    p = peer_state_dir / "session.jsonl"
    try:
        st = p.stat()
    except OSError:
        return None, None
    return float(st.st_mtime), int(st.st_size)


def check_peer_freshness(
    *,
    observer: str,
    peer: str,
    peer_state_dir: Path,
    now: float,
    config: WatchConfig | None = None,
) -> list[StalePeerAlert]:
    """Probe one peer's state dir and return any structural alerts.

    Returns ``[]`` when the peer is healthy — clean cross-monitor
    signal. Returns one alert per failed check (heartbeat AND
    session.jsonl can BOTH fire on a wedged peer that has not
    beat at all for a long time).

    Two checks:

      1. **Heartbeat freshness** — ``heartbeat.json`` ``ts`` vs
         ``now``. Missing / corrupt heartbeat → stale-heartbeat
         alert with ``age_seconds=inf`` semantics expressed as the
         threshold + 1 (so the evidence field carries "no beat").
      2. **session.jsonl growth** — only meaningful when the
         heartbeat IS fresh (otherwise the heartbeat alert covers
         the case). Fires when ``state`` is non-idle AND
         ``session.jsonl`` mtime is older than the idle threshold.

    Pure function (no DB writes, no time.time()): the caller passes
    ``now`` so a test can pin the clock. The caller is also responsible
    for forwarding each returned alert to
    :func:`state_db_alerts.record_alert` and for resolving any prior
    active alert when the peer is now healthy.
    """
    cfg = config or load_watch_config()
    alerts: list[StalePeerAlert] = []

    hb = _read_peer_heartbeat(peer_state_dir)
    last_hb_ts: float | None = None
    peer_state: str | None = None
    if hb is not None:
        raw_ts = hb.get("ts")
        if isinstance(raw_ts, (int, float)) and raw_ts > 0:
            last_hb_ts = float(raw_ts)
        raw_state = hb.get("state")
        if isinstance(raw_state, str):
            peer_state = raw_state

    jsonl_mtime, jsonl_bytes = _peer_session_jsonl_mtime(peer_state_dir)

    # Check 1 — heartbeat freshness. Missing heartbeat counts as
    # infinitely stale (threshold + a clear sentinel age).
    if last_hb_ts is None:
        alerts.append(
            StalePeerAlert(
                observer=observer,
                peer=peer,
                kind=KIND_STALE_HEARTBEAT,
                age_seconds=cfg.heartbeat_threshold_s + 1.0,
                threshold_s=cfg.heartbeat_threshold_s,
                evidence={
                    "peer_state_dir": str(peer_state_dir),
                    "reason": "heartbeat.json missing or unreadable",
                    "last_heartbeat_ts": None,
                    "last_session_jsonl_mtime": jsonl_mtime,
                    "last_session_jsonl_bytes": jsonl_bytes,
                },
            )
        )
    else:
        age = max(0.0, now - last_hb_ts)
        if age > cfg.heartbeat_threshold_s:
            alerts.append(
                StalePeerAlert(
                    observer=observer,
                    peer=peer,
                    kind=KIND_STALE_HEARTBEAT,
                    age_seconds=age,
                    threshold_s=cfg.heartbeat_threshold_s,
                    evidence={
                        "peer_state_dir": str(peer_state_dir),
                        "last_heartbeat_ts": last_hb_ts,
                        "peer_state": peer_state,
                        "last_session_jsonl_mtime": jsonl_mtime,
                        "last_session_jsonl_bytes": jsonl_bytes,
                    },
                )
            )

    # Check 2 — session.jsonl growth. Only fires when heartbeat is
    # fresh AND the peer claims a non-idle state but produced nothing.
    # An idle peer with a quiet transcript is HEALTHY (it correctly
    # reports it is doing nothing) — we don't punish that.
    if (
        last_hb_ts is not None
        and (now - last_hb_ts) <= cfg.heartbeat_threshold_s
        and peer_state in _NON_IDLE_STATES
        and jsonl_mtime is not None
    ):
        jsonl_age = max(0.0, now - jsonl_mtime)
        if jsonl_age > cfg.jsonl_idle_threshold_s:
            alerts.append(
                StalePeerAlert(
                    observer=observer,
                    peer=peer,
                    kind=KIND_STALE_SESSION_JSONL,
                    age_seconds=jsonl_age,
                    threshold_s=cfg.jsonl_idle_threshold_s,
                    evidence={
                        "peer_state_dir": str(peer_state_dir),
                        "last_heartbeat_ts": last_hb_ts,
                        "peer_state": peer_state,
                        "last_session_jsonl_mtime": jsonl_mtime,
                        "last_session_jsonl_bytes": jsonl_bytes,
                    },
                )
            )

    return alerts


__all__ = [
    "KIND_STALE_HEARTBEAT",
    "KIND_STALE_SESSION_JSONL",
    "StalePeerAlert",
    "WatchConfig",
    "check_peer_freshness",
    "load_watch_config",
]
