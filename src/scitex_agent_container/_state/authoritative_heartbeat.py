"""Privacy-safe contract for resident, host-authoritative agent heartbeats."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterable, Mapping, Sequence
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
        "session_jsonl_delta_bytes",
        "subagent_jsonl_delta_bytes",
        "nonce_challenge",
        "nonce_echo",
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


def heartbeat_delta_bytes(
    heartbeat: Mapping[str, object],
) -> tuple[float, float]:
    """Mechanically-measured work evidence from one beat.

    Returns ``(session_jsonl_delta_bytes, subagent_jsonl_delta_bytes)`` —
    file-size growth measured by the writer loop, never self-declared.
    Missing, non-numeric or negative entries read as 0.0 (no evidence).
    These two fields are the ONLY heartbeat content the verdict may read.
    """
    deltas = []
    for key in ("session_jsonl_delta_bytes", "subagent_jsonl_delta_bytes"):
        value = heartbeat.get(key, 0)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            deltas.append(0.0)
        else:
            deltas.append(max(0.0, float(value)))
    session_delta, subagent_delta = deltas
    return session_delta, subagent_delta


NONCE_DIGITS = 16
_NONCE_MODULUS = 10**NONCE_DIGITS


def issue_challenge_nonce() -> str:
    """Issue a random 16-digit challenge nonce (CCT 4309).

    The listen server announces one per agent; the agent echoes it
    agentically (its own session output); listen reflects the observed
    echo in the heartbeat. Zero-padded so the echo is greppable as a
    fixed-width token in session.jsonl.
    """
    import secrets as _secrets

    return f"{_secrets.randbelow(_NONCE_MODULUS):0{NONCE_DIGITS}d}"


def _nonce_token(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != NONCE_DIGITS:
        return None
    if not value.isdigit():
        return None
    return value


def nonce_echo_confirms(heartbeat: Mapping[str, object]) -> bool | None:
    """Dual-confirmation check for one beat (CCT 4309).

    Returns ``None`` when the beat carries no challenge (writer has not
    enrolled in the nonce protocol — deltas alone decide). Otherwise
    ``True`` iff the reflected echo is a byte-exact match of the
    challenge; a missing or mismatched echo is ``False`` (failed
    confirmation), never an error.
    """
    import hmac as _hmac

    challenge = _nonce_token(heartbeat.get("nonce_challenge"))
    if challenge is None:
        return None
    echo = _nonce_token(heartbeat.get("nonce_echo"))
    if echo is None:
        return False
    return _hmac.compare_digest(challenge, echo)


def classify_resident_state(
    beats: Mapping[str, object] | Sequence[Mapping[str, object]],
    *,
    now: float,
    window_s: float = 600.0,
) -> str:
    """Binary work判定: WORKING or DEAD, no intermediate states.

    OPERATOR ORDERS 2026-10-07 (CCT 4276/4289/4291/4299/4305/4309): an
    agent is either WORKING or DEAD. The verdict reads ONLY the two
    mechanically-measured delta fields — ``session_jsonl_delta_bytes``
    and ``subagent_jsonl_delta_bytes`` (file-size growth, never pane
    rendering, so TUI presence alone can never read WORKING) — plus the
    nonce challenge/response pair. NO liveness, NO counters
    (``progress_seq``, ``turns/tools_completed``), NO phase
    (``state``, ``current_phase``), NO card identity. Nothing
    self-declared, nothing counted.

    ``beats`` is the beat history for one agent (a single beat mapping
    is accepted and treated as a one-beat history). Beats fire every
    ~60s; ``window_s`` is the 10-minute work window: the in-window
    deltas are SUMMED, and a positive sum reads WORKING. A lone
    delta-positive beat inside the window implies a positive sum, so
    latest-only callers degrade exactly to "delta > 0 recently".

    Dual confirmation (CCT 4309): where the history's latest in-window
    beat carries a challenge, its reflected echo must match, or the
    verdict is DEAD even with positive deltas. Beats with no challenge
    are decided on deltas alone (writer not yet enrolled).

    Everything else — zero windowed deltas, no in-window beat, a stale
    beat, a failed echo — is DEAD.
    """
    if isinstance(beats, Mapping):
        history = [beats]
    else:
        history = list(beats)
    current = float(now)
    cutoff = current - float(window_s)
    total = 0.0
    latest_challenged: Mapping[str, object] | None = None
    latest_observed = float("-inf")
    for beat in history:
        try:
            observed_at = _number(beat, "observed_at")
        except AuthoritativeHeartbeatError:
            continue
        if observed_at < cutoff or observed_at > current + DEFAULT_FUTURE_SKEW_S:
            continue
        session_delta, subagent_delta = heartbeat_delta_bytes(beat)
        total += session_delta + subagent_delta
        if observed_at >= latest_observed and _nonce_token(
            beat.get("nonce_challenge")
        ) is not None:
            latest_observed = observed_at
            latest_challenged = beat
    if total <= 0.0:
        return "dead"
    if latest_challenged is not None and not nonce_echo_confirms(
        latest_challenged
    ):
        return "dead"
    return "working"


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
    "heartbeat_delta_bytes",
    "issue_challenge_nonce",
    "nonce_echo_confirms",
    "read_card_lease",
    "select_federated_heartbeats",
    "validate_heartbeat",
    "write_card_lease",
]
