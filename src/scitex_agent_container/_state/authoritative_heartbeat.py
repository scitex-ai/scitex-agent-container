"""Privacy-safe contract for resident, host-authoritative agent heartbeats."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

ALLOWED_FIELDS = frozenset(
    {
        "agent_id",
        "spec_id",
        "host",
        "runtime",
        "harness",
        "engine",
        "model",
        "session_id",
        "boot_id",
        "seq",
        "monotonic_ns",
        "observed_at",
        "progress_at",
        "progress_seq",
        "state",
        "lease_expires_at",
        "card_id",
        "card_role",
    }
)
RESIDENT_STATES = frozenset({"idle", "active", "blocked"})
CARD_ROLES = frozenset({"", "developer", "reviewer"})
DEFAULT_FUTURE_SKEW_S = 5.0
DEFAULT_MAX_LEASE_S = 120.0


class AuthoritativeHeartbeatError(ValueError):
    """A heartbeat failed identity, ordering, time, or privacy validation."""


def write_card_lease(
    state_dir: Path,
    *,
    card_id: str,
    role: str,
    expires_at: float,
    now: float | None = None,
) -> None:
    """Atomically publish one privacy-safe developer/reviewer lease."""
    from .._runners._atomic import atomic_write_text

    card_id = str(card_id or "").strip()
    role = str(role or "").strip()
    if not card_id or role not in CARD_ROLES.difference({""}):
        raise AuthoritativeHeartbeatError("invalid card_role/card_id lease")
    current = time.time() if now is None else float(now)
    if not math.isfinite(float(expires_at)) or float(expires_at) <= current:
        raise AuthoritativeHeartbeatError("card lease must expire in the future")
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_dir / "card-lease.json",
        json.dumps(
            {"card_id": card_id, "role": role, "expires_at": float(expires_at)},
            sort_keys=True,
        ),
    )


def read_card_lease(state_dir: Path, *, now: float | None = None) -> tuple[str, str]:
    """Return the active ``(card_id, role)`` or an empty expired/absent lease."""
    try:
        payload = json.loads(
            (Path(state_dir) / "card-lease.json").read_text(encoding="utf-8")
        )
        card_id = str(payload.get("card_id") or "").strip()
        role = str(payload.get("role") or "").strip()
        expires_at = float(payload.get("expires_at"))
    except (OSError, ValueError, TypeError, AttributeError):
        return "", ""
    if (
        not card_id
        or role not in CARD_ROLES.difference({""})
        or expires_at <= (time.time() if now is None else float(now))
    ):
        return "", ""
    return card_id, role


def clear_card_lease(state_dir: Path) -> None:
    """Remove the local Card lease projection after Cards revokes it."""
    (Path(state_dir) / "card-lease.json").unlink(missing_ok=True)


def _text(payload: Mapping[str, object], key: str, *, optional: bool = False) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise AuthoritativeHeartbeatError(f"invalid {key}")
    return value.strip()


def _integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise AuthoritativeHeartbeatError(f"invalid {key}")
    return value


def _number(payload: Mapping[str, object], key: str) -> float:
    value = payload.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise AuthoritativeHeartbeatError(f"invalid {key}")
    return float(value)


def validate_heartbeat(
    payload: Mapping[str, object],
    *,
    expected_agent: str,
    expected_host: str,
    now: float,
    previous: Mapping[str, object] | None = None,
    future_skew_s: float = DEFAULT_FUTURE_SKEW_S,
    max_lease_s: float = DEFAULT_MAX_LEASE_S,
) -> dict[str, object]:
    """Validate one canonical heartbeat and return a plain privacy-safe dict."""
    unsupported = set(payload).difference(ALLOWED_FIELDS)
    if unsupported:
        raise AuthoritativeHeartbeatError(
            f"unsupported field(s): {', '.join(sorted(unsupported))}"
        )
    agent = _text(payload, "agent_id")
    host = _text(payload, "host")
    if agent != expected_agent or host != expected_host:
        raise AuthoritativeHeartbeatError("heartbeat identity mismatch")
    for key in (
        "spec_id",
        "runtime",
        "harness",
        "engine",
        "model",
        "session_id",
        "boot_id",
    ):
        _text(payload, key)
    card_id = _text(payload, "card_id", optional=True)
    card_role = _text(payload, "card_role", optional=True)
    if card_role not in CARD_ROLES or bool(card_id) != bool(card_role):
        raise AuthoritativeHeartbeatError("invalid card_role/card_id lease")
    state = _text(payload, "state")
    if state not in RESIDENT_STATES:
        raise AuthoritativeHeartbeatError(f"invalid resident state: {state!r}")
    seq = _integer(payload, "seq")
    monotonic_ns = _integer(payload, "monotonic_ns")
    progress_seq = _integer(payload, "progress_seq")
    observed_at = _number(payload, "observed_at")
    progress_at = _number(payload, "progress_at")
    lease_expires_at = _number(payload, "lease_expires_at")
    if observed_at > float(now) + float(future_skew_s):
        raise AuthoritativeHeartbeatError("heartbeat timestamp is in the future")
    if progress_at > observed_at:
        raise AuthoritativeHeartbeatError("progress timestamp exceeds observation")
    lease_s = lease_expires_at - observed_at
    if lease_s <= 0 or lease_s > float(max_lease_s):
        raise AuthoritativeHeartbeatError("invalid heartbeat lease interval")
    if previous is not None:
        previous_boot = _text(previous, "boot_id")
        previous_observed = _number(previous, "observed_at")
        if observed_at <= previous_observed:
            raise AuthoritativeHeartbeatError("heartbeat timestamp is duplicate/out-of-order")
        if previous_boot == _text(payload, "boot_id"):
            for key in (
                "spec_id",
                "runtime",
                "harness",
                "engine",
                "model",
                "session_id",
            ):
                if _text(previous, key) != _text(payload, key):
                    raise AuthoritativeHeartbeatError(
                        f"heartbeat stable identity changed within boot: {key}"
                    )
            if seq <= _integer(previous, "seq"):
                raise AuthoritativeHeartbeatError("heartbeat sequence is duplicate/out-of-order")
            if monotonic_ns <= _integer(previous, "monotonic_ns"):
                raise AuthoritativeHeartbeatError("heartbeat monotonic time regressed")
            if progress_seq < _integer(previous, "progress_seq"):
                raise AuthoritativeHeartbeatError("heartbeat progress sequence regressed")
        elif seq > 1:
            raise AuthoritativeHeartbeatError("new heartbeat boot must reset sequence")
    return dict(payload)


def classify_resident_state(
    heartbeat: Mapping[str, object],
    *,
    now: float,
    process_alive: bool | None,
    federation_connected: bool,
    progress_stale_s: float,
) -> str:
    """Classify one validated latest beat without collapsing UNKNOWN into death."""
    lease_expires_at = _number(heartbeat, "lease_expires_at")
    if process_alive is False:
        return "dead"
    if not federation_connected or float(now) > lease_expires_at:
        return "disconnected"
    state = _text(heartbeat, "state")
    if state == "blocked":
        return "blocked"
    if state == "active":
        progress_at = _number(heartbeat, "progress_at")
        if float(now) - progress_at > float(progress_stale_s):
            return "stalled"
        return "active"
    return "idle"


def select_federated_heartbeats(
    beats: Iterable[Mapping[str, object]], *, now: float
) -> list[dict[str, object]]:
    """Select one lease per agent and reject overlapping cross-host authority."""
    grouped: dict[str, list[dict[str, object]]] = {}

    def numeric(row: Mapping[str, object], key: str) -> float:
        value = row.get(key)
        return (
            float(value)
            if not isinstance(value, bool) and isinstance(value, (int, float))
            else -1.0
        )

    for beat in beats:
        agent = str(beat.get("agent_id") or "").strip()
        if not agent:
            continue
        grouped.setdefault(agent, []).append(dict(beat))
    selected = []
    for agent in sorted(grouped):
        rows = grouped[agent]
        active = [
            row for row in rows if numeric(row, "lease_expires_at") >= float(now)
        ]
        authorities = {(row.get("host"), row.get("boot_id")) for row in active}
        if len(authorities) > 1:
            raise AuthoritativeHeartbeatError(
                f"overlapping heartbeat authorities for agent {agent!r}"
            )
        candidates = active or rows
        selected.append(
            max(candidates, key=lambda row: numeric(row, "observed_at"))
        )
    return selected


__all__ = [
    "ALLOWED_FIELDS",
    "AuthoritativeHeartbeatError",
    "CARD_ROLES",
    "RESIDENT_STATES",
    "classify_resident_state",
    "clear_card_lease",
    "read_card_lease",
    "select_federated_heartbeats",
    "validate_heartbeat",
    "write_card_lease",
]
