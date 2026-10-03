"""One scoped ledger read and passive, privacy-safe handshake projections."""

from __future__ import annotations

import json

from scitex_dev.status import is_exchange_id
from scitex_dev.store import Query, eq, is_in

from .._state._agentic_handshake import OPERATION
from ..runtimes._activity_source_identity import SOURCE_KIND, activity_source_id
from ..runtimes._turn_exchange_ledger import _store
from ._handshake_observation import handshake_observation
from ._observation_contract import ExchangeObservation, HandshakeSnapshot, LedgerPage

# A resource page bound, never a reply-freshness or admission threshold.
LEDGER_PAGE_SIZE = 256


def read_handshake_page(names, *, store_factory=None, limit=LEDGER_PAGE_SIZE):
    """Read existing exchange rows only; no open/advance/ACK/proof operation."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("ledger page size must be a positive integer")
    wanted = tuple(sorted(set(names)))
    if not wanted:
        return [], LedgerPage(limit=limit, returned=0, complete=True)
    store = (store_factory or _store)()
    try:
        records = store.search(
            Query()
            .where(eq("initiator", "sac.listen"), is_in("responder", wanted))
            .ordered_by("opened_at")
            .limited(limit + 1)
        )
        values = [dict(record.values) for record in records]
    finally:
        store.close()
    return values[:limit], LedgerPage(
        limit=limit, returned=min(len(values), limit), complete=len(values) <= limit
    )


def _decode(row, authority):
    try:
        contract = json.loads(row["operation"])
        state = json.loads(row["message"])
        target = contract["target"]
        if (
            row["initiator"] != "sac.listen"
            or row["responder"] != authority.agent
            or contract["schema"] != OPERATION
            or not is_exchange_id(row["exchange_id"])
            or row["kind"] != "http"
            or type(row["code"]) is not int
            or type(row["final"]) is not bool
            or not isinstance(state, dict)
            or target != authority.model_dump(exclude={"source"})
            or activity_source_id(target, contract["source_identity"])
            != authority.source.identity
            or authority.source.kind != SOURCE_KIND
        ):
            return None
        return contract, state
    except (KeyError, TypeError, ValueError):
        return None


def _exchange(row, authority, now):
    decoded = _decode(row, authority)
    if decoded is None:
        return None
    contract, state = decoded
    try:
        clocks = handshake_observation(contract, state, observed_at=now)
        if clocks["issued_at"] is None or clocks["deadline"] is None:
            return None
        expired = now > clocks["deadline"]
        success = (
            row["final"] is True
            and row["code"] == 200
            and state.get("proven") is True
            and clocks["verified_at"] is not None
        )
        if not success:
            clocks["verified_at"] = None
            clocks["verified_age_s"] = None
        phase = "expired" if expired else "pending"
        proven = None
        if row["final"] and not expired:
            phase = (
                "proven"
                if success
                else "not_proven"
                if state.get("proven") is False
                else "unknown"
            )
            proven = (
                True if success else False if state.get("proven") is False else None
            )
        return ExchangeObservation(
            exchange_id=row["exchange_id"],
            authority=authority,
            phase=phase,
            proven=proven,
            **clocks,
        )
    except (KeyError, TypeError, ValueError):
        return None


def handshake_snapshot(authority, rows, page, *, now):
    """Separate current exchange from immutable, same-authority last success."""
    if not page.complete:
        return HandshakeSnapshot(
            state="unknown", reason="history_incomplete", page=page
        )
    for row in rows:
        decoded = _decode(row, authority)
        if decoded is not None:
            clocks = handshake_observation(*decoded, observed_at=now)
            if clocks["issued_at"] is None or clocks["deadline"] is None:
                return HandshakeSnapshot(
                    state="unknown", reason="clock_uncertain", page=page
                )
    exchanges = [
        value for row in rows if (value := _exchange(row, authority, now)) is not None
    ]
    successes = [value for value in exchanges if value.verified_at is not None]
    latest = sorted(exchanges, key=lambda value: value.issued_at, reverse=True)
    proven = sorted(successes, key=lambda value: value.verified_at, reverse=True)
    if (len(latest) > 1 and latest[0].issued_at == latest[1].issued_at) or (
        len(proven) > 1 and proven[0].verified_at == proven[1].verified_at
    ):
        return HandshakeSnapshot(state="unknown", reason="ambiguous_order", page=page)
    return HandshakeSnapshot(
        state="observed",
        reason="",
        page=page,
        current_exchange=latest[0] if latest else None,
        last_verified_reply=proven[0] if proven else None,
    )
