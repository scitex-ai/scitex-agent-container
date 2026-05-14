"""Atomic per-agent A2A port allocator.

Per-agent A2A ports are an IPC mechanism between ``sac listen`` (the
single externally-visible host port, default 7878) and each agent's
in-process sidecar runner. Operators should never hand-pick them —
collisions are silent (second binder fails) and the only sane default
is auto-allocation.

This module owns the ``a2a_ports`` table in ``state.db``. The table
maintains a ``(agent_name, port)`` claim with a UNIQUE constraint on
``port`` so concurrent claims can never hand the same port to two
agents. Claims are idempotent: re-claiming for the same agent returns
the existing port.

Resolution order at agent_start:

  1. Spec author pinned an explicit int → that int is recorded as the
     claim. Collisions raise (operator intent disagrees with reality —
     fail loudly).
  2. Spec author set ``port: auto`` (or left a2a unset) → allocator
     scans ``range_`` ascending and persists the first unused port.

Range defaults to ``(19000, 19999)``. Override via
``~/.scitex/agent-container/config.yaml``::

    a2a:
      port_range: [19000, 19999]

The allocator only owns the **claim**. Actual port binding happens
inside the runner (which exits non-zero if the kernel refuses the
bind); a sweeper (sac agents stop) calls ``release_port`` so claims
don't leak across runs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .state_db import init_schema, open_db

# Built-in default range. Tuned to sit above the IANA dynamic range
# floor (49152) is overkill for a single-host loopback IPC channel;
# 19xxx is high enough to avoid common dev ports (8080/8443/9000/9090)
# yet low enough to leave the ephemeral pool intact for outbound
# sockets. Operators override via config.yaml.
DEFAULT_RANGE: tuple[int, int] = (19000, 19999)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS a2a_ports (
    name        TEXT PRIMARY KEY,
    port        INTEGER NOT NULL UNIQUE,
    claimed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_a2a_ports_port ON a2a_ports(port);
"""


def _ensure_schema(db_path: Path | None) -> None:
    """Create ``a2a_ports`` if missing. ``state.db`` core schema first.

    Kept separate from ``state_db._SCHEMA_REGISTRY`` so a partial
    rollback of this feature doesn't leave foreign-key debris on the
    main ``instances`` table.
    """
    init_schema(db_path)
    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)


def _resolve_range(range_: tuple[int, int] | None) -> tuple[int, int]:
    """Pick the active port range.

    Precedence: explicit ``range_`` arg > ``a2a.port_range`` in
    ``config.yaml`` > module ``DEFAULT_RANGE``. Config-file load is
    tolerant: malformed entries fall through to the default rather
    than blocking agent_start.
    """
    if range_ is not None:
        return range_
    # stx-allow: fallback (reason: config.yaml is operator-edited and
    # may be malformed; a broken range key must not block allocation —
    # fall back to the built-in default.)
    try:
        from .host_config import _default_config_path

        path = _default_config_path()
        if path.is_file():
            import yaml

            raw = yaml.safe_load(path.read_text()) or {}
            a2a_raw = raw.get("a2a") or {}
            pr = a2a_raw.get("port_range")
            if (
                isinstance(pr, (list, tuple))
                and len(pr) == 2
                and all(isinstance(x, int) for x in pr)
                and pr[0] < pr[1]
            ):
                return (int(pr[0]), int(pr[1]))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass
    return DEFAULT_RANGE


def _now_iso() -> str:
    """Local copy avoids pulling state_db's full surface into hot path."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_port(agent_name: str, *, db_path: Path | None = None) -> int | None:
    """Return the currently-claimed port for ``agent_name``, else None.

    Fast read — no schema mutation when the table is missing (treated
    as 'no claim').
    """
    _ensure_schema(db_path)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT port FROM a2a_ports WHERE name=?", (agent_name,)
        ).fetchone()
        return int(row["port"]) if row else None


def claim_port(
    agent_name: str,
    *,
    range_: tuple[int, int] | None = None,
    explicit: int | None = None,
    db_path: Path | None = None,
) -> int:
    """Atomically claim a free port for ``agent_name``.

    Args:
        agent_name: The spec's ``metadata.name``. Idempotent: a second
            call for the same agent returns the existing port without
            mutating state.
        range_: ``(lo, hi)`` inclusive scan range. Falls back to
            ``config.yaml``'s ``a2a.port_range``, then ``DEFAULT_RANGE``.
        explicit: When set, persist this specific port for the agent
            (honours an operator-pinned ``spec.a2a.port`` int). Raises
            ``RuntimeError`` if the port is already claimed by another
            agent.
        db_path: Override state.db location (tests).

    Returns:
        The port number now bound to ``agent_name``.

    Raises:
        RuntimeError: When no free port remains in ``range_`` (or when
            an ``explicit`` port collides with a foreign claim).
    """
    _ensure_schema(db_path)
    lo, hi = _resolve_range(range_)
    now = _now_iso()

    with open_db(db_path) as conn:
        # Idempotent fast path: same agent → return existing claim.
        row = conn.execute(
            "SELECT port FROM a2a_ports WHERE name=?", (agent_name,)
        ).fetchone()
        if row:
            existing = int(row["port"])
            if explicit is not None and explicit != existing:
                # Operator changed the pin between starts. Update the
                # claim to the new explicit port (releasing the old).
                conn.execute("DELETE FROM a2a_ports WHERE name=?", (agent_name,))
            else:
                return existing

        if explicit is not None:
            # Honour the pin. Collision = the operator's spec disagrees
            # with reality; raise so the misconfiguration is visible.
            clash = conn.execute(
                "SELECT name FROM a2a_ports WHERE port=?", (explicit,)
            ).fetchone()
            if clash:
                raise RuntimeError(
                    f"a2a port {explicit} already claimed by "
                    f"{clash['name']!r}; cannot pin for {agent_name!r}"
                )
            conn.execute(
                "INSERT INTO a2a_ports (name, port, claimed_at) VALUES (?, ?, ?)",
                (agent_name, explicit, now),
            )
            return int(explicit)

        # Auto: ascending scan + UNIQUE-constraint optimistic insert.
        # The transaction (open_db wraps commit/rollback) plus
        # UNIQUE(port) means two threads racing on the same candidate
        # serialise: one wins the INSERT, the other catches
        # IntegrityError and re-scans.
        for candidate in range(lo, hi + 1):
            try:
                conn.execute(
                    "INSERT INTO a2a_ports (name, port, claimed_at) VALUES (?, ?, ?)",
                    (agent_name, candidate, now),
                )
                return candidate
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError(
            f"no free a2a port in range [{lo}, {hi}] (all claimed); "
            "extend a2a.port_range in ~/.scitex/agent-container/config.yaml"
        )


def release_port(agent_name: str, *, db_path: Path | None = None) -> bool:
    """Drop the claim. Idempotent — returns True iff a row was deleted."""
    _ensure_schema(db_path)
    with open_db(db_path) as conn:
        cur = conn.execute("DELETE FROM a2a_ports WHERE name=?", (agent_name,))
        return cur.rowcount > 0


def list_claims(*, db_path: Path | None = None) -> list[dict]:
    """Return every active claim. Used by ``sac agents list`` to enrich rows."""
    _ensure_schema(db_path)
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT name, port, claimed_at FROM a2a_ports ORDER BY port"
        ).fetchall()
        return [dict(r) for r in rows]
