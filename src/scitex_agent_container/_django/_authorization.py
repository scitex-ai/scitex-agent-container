"""Scoped-visibility and operator authorization for the Agents GUI.

Two independent decisions, each with a single honest answer:

1. **SCOPE** (which agents a browser sees). An ordinary caller sees ONLY the
   agents on the node this GUI serves. A row from ``GET /agents`` is a local
   agent iff it carries no ``host`` field, or its ``host`` names this node.
   Remote (other-node) agents are invisible to a non-operator; an operator who
   has been *explicitly authorized for the cross-host fleet* sees them, flagged.

2. **CONTROL** (who may start/stop/restart). Never the same as "sees".
   - own-scope agent: caller must be in ``SCITEX_AGENT_CONTAINER_LIFECYCLE_OPERATORS``.
   - cross-host agent: caller must be in
     ``SCITEX_AGENT_CONTAINER_CROSSHOST_OPERATORS`` — a STRICTER, separate
     list — and the action is recorded in the audit trail. Cross-user fleet
     access is therefore explicit, separable, and audited.

The identity is supplied by the deployment: the mounted host passes an
authenticated ``request.user``; the standalone server passes a declared
``SCITEX_AGENT_CONTAINER_GUI_IDENTITY``. An empty/unknown identity is a
non-operator — it can see own-scope agents (read-only) and nothing else.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._constants import (
    AUDIT_LOG_ENV,
    CROSSHOST_OPERATORS_ENV,
    IDENTITY_ENV,
    OPERATORS_ENV,
)


def _parse_allowlist(env: str) -> frozenset[str]:
    return frozenset(
        part.strip() for part in os.environ.get(env, "").split(",") if part.strip()
    )


def local_hostname() -> str:
    """Best-effort identity of THIS node for the scope test."""
    try:
        return socket.gethostname()
    except OSError:  # stx-allow: fallback (reason: container may lack a resolvable hostname)
        return ""


def _own_scope_hosts() -> frozenset[str]:
    hosts = {"", "local", "localhost", "127.0.0.1", "::1"}
    name = local_hostname()
    if name:
        hosts.add(name)
    return frozenset(hosts)


def _node_of(row: dict[str, Any]) -> str:
    """The node an agent row belongs to, most-authoritative first.

    ``turn_url`` is authoritative: the listener builds it from the agent's real
    placement, so its hostname names the node (``http://scitex-01:.../v1/turn``
    → ``scitex-01``). The registry is fleet-wide, so a row with no explicit
    ``host`` can still live on another node — the ``host`` field is therefore
    only a fallback, not the signal.
    """
    turn_url = row.get("turn_url")
    if isinstance(turn_url, str) and turn_url:
        try:
            host = urlsplit(turn_url).hostname
        except ValueError:
            host = None
        if host:
            return host
    host = row.get("host")
    if host in (None, ""):
        return ""
    return str(host)


def is_own_scope(row: dict[str, Any]) -> bool:
    """True iff the row names this node (or is unattributed → local)."""
    node = _node_of(row)
    if node == "":
        return True
    return node in _own_scope_hosts()


def resolve_identity(request: Any) -> str:
    """The acting identity: a Django-authenticated user, else the declared one."""
    user = getattr(request, "user", None)
    username = getattr(user, "username", None)
    if username:
        return str(username)
    return os.environ.get(IDENTITY_ENV, "").strip()


def _audit_path() -> Path:
    env = os.environ.get(AUDIT_LOG_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path("~/.scitex/agent-container/runtime/gui-audit.log").expanduser()


def record_audit(event: dict[str, Any]) -> Path:
    """Append one structured line to the cross-host authorization audit trail."""
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "epoch": time.time(),
        **event,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def can_control(
    identity: str,
    *,
    cross_host: bool,
    request: Any = None,
    agent: str = "",
) -> bool:
    """Decide control for an identity, auditing cross-host grants.

    ``cross_host`` selects the list. A granted cross-host control is recorded
    the moment it is DECIDED true (not just at execution) so the trail is a
    faithful log of authorizations, not of successes.
    """
    if not identity:
        return False
    if cross_host:
        if identity in _parse_allowlist(CROSSHOST_OPERATORS_ENV):
            record_audit(
                {
                    "event": "crosshost_control_granted",
                    "identity": identity,
                    "agent": agent,
                    "path": getattr(request, "path", None) if request is not None else None,
                    "method": getattr(request, "method", None) if request is not None else None,
                }
            )
            return True
        return False
    return identity in _parse_allowlist(OPERATORS_ENV)


def fleet_visibility(identity: str) -> str:
    """``"own"`` or ``"crosshost"`` — what the browser is allowed to SEE."""
    if identity and identity in _parse_allowlist(CROSSHOST_OPERATORS_ENV):
        return "crosshost"
    return "own"


def scope_rows(rows: list[dict[str, Any]], identity: str) -> list[dict[str, Any]]:
    """Filter the /agents rows down to what ``identity`` may see, tagging scope."""
    visibility = fleet_visibility(identity)
    out: list[dict[str, Any]] = []
    for row in rows:
        own = is_own_scope(row)
        if not own and visibility != "crosshost":
            continue
        tagged = dict(row)
        tagged["scope"] = "cross-host" if not own else "own"
        out.append(tagged)
    return out


__all__ = [
    "can_control",
    "fleet_visibility",
    "is_own_scope",
    "local_hostname",
    "record_audit",
    "resolve_identity",
    "scope_rows",
]
