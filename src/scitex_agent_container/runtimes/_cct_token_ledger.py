"""Record this agent's claim on its bot token, as it starts. Never fatal.

The start-time half of :mod:`.._state.state_db_token_owner`: resolve which bot
this agent takes — with :func:`._cct_token_resolution.resolve_cct_token`, the
same derivation the writer uses, never a second one — and write
``fingerprint -> (agent, host, pid, started_at)`` into the per-host ledger.

WRITE ONLY. Nothing reads this to make a decision, no start is gated on it,
and this function's return value is discarded by its caller. Enforcement is a
separate change; see the store's docstring for why the record comes first.

NEVER RAISES, and that is the load-bearing property, not a nicety. The store
is PostgreSQL-only and RAISES when the database is unreachable — correct for
the store, and unacceptable on a path attached to every start in the fleet. A
ledger that can refuse a boot is strictly worse than a missing ledger, so an
unreachable store here becomes a printed warning and the agent starts.

Same reasoning, same shape and the same call site as
:func:`._cct_rail_alarm.check_cct_rail_at_start`, which is one line above it in
:mod:`.._lifecycle._start`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

#: What :func:`record_token_claim_at_start` did, for tests and for a caller
#: that wants to say so.
CLAIM_RECORDED = "recorded"
#: No token resolves for this agent, so there is no claim to record. Includes
#: the deliberately tokenless (the handyman pattern) and the merely mute.
CLAIM_SKIPPED = "skipped"
#: A claim existed and could not be written down. Printed loudly; the start
#: proceeds regardless.
CLAIM_FAILED = "failed"


def _resolve_host() -> str:
    """This host's canonical label, matching the rest of sac's records."""
    # stx-allow: fallback (reason: the host is a LABEL on a ledger row; a resolver import/lookup failure must degrade to the plain hostname, never take down the start this is attached to)
    try:
        from .._state.state_db_hostname import resolve_host

        return resolve_host(None)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        import socket

        return socket.gethostname()


def record_token_claim_at_start(
    config,
    *,
    dest: Path | None = None,
    pid: int | None = None,
    now: float | None = None,
    err_stream: Any = None,
) -> str:
    """Write this agent's bot-token claim to the per-host ledger.

    Returns :data:`CLAIM_RECORDED`, :data:`CLAIM_SKIPPED` or
    :data:`CLAIM_FAILED`. Never raises.

    ``dest`` is the agent's materialised home; omit it and it is resolved via
    :func:`._cct_rail_verdict.materialised_home`, exactly as the rail check
    does — reading it matters because a token folded there by a project
    ``.envrc`` is precedence #1, and an agent whose bot arrives that way is
    every bit as capable of colliding as one that resolved a pool slot.

    Only the FINGERPRINT is written. The resolution holds a value long enough
    to hash it and no longer, and nothing in this module ever sees one.
    """
    stream = err_stream if err_stream is not None else sys.stderr
    # stx-allow: fallback (reason: this is a bookkeeping write attached to every start in the fleet; a bug in the resolution, an unreachable PostgreSQL, or a schema surprise must degrade to a printed line, never take down the start it was added to observe)
    try:
        from .._state.state_db_token_owner import record_token_owner
        from ._cct_rail_verdict import materialised_home
        from ._cct_token_resolution import resolve_cct_token

        home = dest if dest is not None else materialised_home(config)
        resolution = resolve_cct_token(config, dest=home)
        if not resolution.claims_a_token:
            return CLAIM_SKIPPED
        record_token_owner(
            token_fp=resolution.token_fp,
            agent=resolution.agent,
            host=_resolve_host(),
            pid=pid if pid is not None else os.getpid(),
            started_at=now,
            source=resolution.source,
            slot=resolution.slot,
        )
        return CLAIM_RECORDED
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        print(
            f"[cct-ledger] {getattr(config, 'name', '?')!r}: could not record "
            f"this agent's bot-token claim — {exc}. THE AGENT STARTS NORMALLY; "
            "only the ownership ledger is missing this claim, so "
            "'who holds this bot?' will have to be answered by scanning /proc "
            "(`sac doctor --pollers`) until the next successful start.",
            file=stream,
        )
        return CLAIM_FAILED


__all__ = [
    "CLAIM_FAILED",
    "CLAIM_RECORDED",
    "CLAIM_SKIPPED",
    "record_token_claim_at_start",
]
