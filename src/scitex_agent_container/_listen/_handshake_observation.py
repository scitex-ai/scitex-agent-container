"""Separate source/server clocks for one immutable native handshake receipt."""

from __future__ import annotations

import math

from ..runtimes._codex_activity import CodexActivityError


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number > 0 else None


def _observed(value, now):
    value = _time(value)
    return value if now is not None and value is not None and value <= now else None


def require_observation_time(contract, now):
    """Refuse an invalid/regressed server clock before current-proof decisions."""
    current = _time(now)
    issued = _time(contract.get("issued_at"))
    deadline = _time(contract.get("deadline"))
    if (
        current is None
        or issued is None
        or deadline is None
        or current < issued
        or deadline <= issued
    ):
        raise CodexActivityError("The handshake observation clock is not valid.")
    return current


def handshake_observation(contract, status, *, observed_at):
    """Project valid measured ages; absent/future/nonfinite evidence is unknown.

    The caller must fence the exact native target/source before exposing a
    historical success. A deadline may be in the future: it is a lease bound,
    not evidence that work happened. Its remaining interval stays signed.
    """
    now = _time(observed_at)
    issued = _observed(contract.get("issued_at"), now)
    deadline = _time(contract.get("deadline"))
    if issued is None or deadline is None or deadline <= issued:
        deadline = None
    proof = status.get("tool_proof")
    completed = (
        _observed(proof.get("completed_at"), now) if isinstance(proof, dict) else None
    )
    if (
        issued is None
        or deadline is None
        or completed is None
        or not issued <= completed <= deadline
    ):
        completed = None
    verified = _observed(status.get("verified_at"), now)
    if (
        status.get("proven") is not True
        or completed is None
        or verified is None
        or verified < completed
    ):
        verified = None
    return {
        "issued_at": issued,
        "deadline": deadline,
        "tool_completed_at": completed,
        "verified_at": verified,
        "observed_at": now,
        "handshake_lease_s": deadline - issued if deadline is not None else None,
        "issued_age_s": now - issued if issued is not None else None,
        "tool_completed_age_s": now - completed if completed is not None else None,
        "verified_age_s": now - verified if verified is not None else None,
        "lease_remaining_s": deadline - now
        if deadline is not None and now is not None
        else None,
    }


def server_verification_time(contract, proof, *, observed_at):
    """Stamp a genuine observed completion using the server's validated clock."""
    now = require_observation_time(contract, observed_at)
    projected = handshake_observation(
        contract,
        {
            "proven": True,
            "verified_at": now,
            "tool_proof": {"completed_at": proof.completed_at},
        },
        observed_at=now,
    )
    if projected["verified_at"] is None:
        raise CodexActivityError(
            "The native completion has no valid verification clock."
        )
    return projected["verified_at"]
