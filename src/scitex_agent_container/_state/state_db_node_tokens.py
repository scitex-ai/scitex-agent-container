"""Authenticated node bearer-token primitives (WI-2 ACL, handoff §4).

Split out of :mod:`.state_db_nodes` to stay under the per-file line
cap (GITIGNORED/REFACTORING.md tracks this move). Re-exported from
``state_db_nodes`` so the existing import surface stays unchanged:

    from scitex_agent_container._state.state_db_nodes import (
        mint_node_token, resolve_node_token, list_node_tokens,
    )

Per-node bearer tokens minted at registration (:func:`mint_node_token`).
The listen server resolves an incoming ``Authorization: Bearer <token>``
to a node name (:func:`resolve_node_token`). With this in place,
``check_send_acl`` enforces "identity cannot be spoofed via a metadata
field" — when a per-node bearer is presented,
``params.metadata.from_agent`` MUST match the bearer's resolved name;
mismatch → 403.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

__all__ = [
    "list_node_tokens",
    "mint_node_token",
    "resolve_node_token",
]

# 256 bits of entropy. URL-safe base64 → ~43 chars.
_TOKEN_BYTES = 32


def mint_node_token(*, name: str, db_path: Path | None = None) -> str:
    """Return the bearer token for ``name``, minting one if absent.

    Idempotent: re-registration returns the existing token rather
    than rotating, so an active agent's ``Authorization: Bearer ...``
    header keeps working across a re-register. Rotation, when needed,
    is a separate operation (not implemented here).

    Raises ``ValueError`` if ``name`` is empty.
    """
    if not name:
        raise ValueError("mint_node_token: name must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT token FROM node_tokens WHERE name = ?", (name,)
        ).fetchone()
        if existing is not None:
            return str(existing["token"])
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = time.time()
        conn.execute(
            "INSERT INTO node_tokens (name, token, created_at) VALUES (?, ?, ?)",
            (name, token, now),
        )
    return token


def resolve_node_token(
    *,
    token: str,
    db_path: Path | None = None,
) -> str | None:
    """Map a bearer token back to a node name; ``None`` if unknown.

    Returns ``None`` for an empty token (defence-in-depth — the
    middleware already rejects requests with no Authorization
    header, but we never resolve ``""`` to a real identity).
    """
    if not token:
        return None
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM node_tokens WHERE token = ?", (token,)
        ).fetchone()
    if row is None:
        return None
    return str(row["name"])


def list_node_tokens(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return ``[{name, created_at}, ...]`` over every minted token.

    Observability surface for the host operator. The token value
    itself is deliberately NOT returned — that would defeat the
    purpose of storing it as a secret.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute("SELECT name, created_at FROM node_tokens ORDER BY name ASC")
        return [
            {"name": str(r["name"]), "created_at": float(r["created_at"])}
            for r in cur.fetchall()
        ]
