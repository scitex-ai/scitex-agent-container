"""ACL-decision dispatch for ``sac a2a {unblock,block,grant}`` (task #27 PR B).

Extracted from ``a2a_group.py`` to keep that file under the 512-line
cap. The CLI verbs in ``a2a_group.py`` call into here; this module
owns the in-SIF detection + routing logic.

Routing rule:

* In-SIF → POST to host listen via :func:`_state._acl_broker_client.
  broker_acl_decision`. The decision lands on the HOST'S state.db
  (where ``sac listen`` runs), NOT the per-container copy that would
  otherwise be a silent no-op (lead FUTURE item 4, operator
  greenlit Q5 on 2026-06-01).
* Bare host → call the local DB-only helpers in
  :mod:`_state.grant_flush` directly. The lead runs ``sac listen``
  on the bare host so a local write IS the host write — no HTTP
  hop needed.

Detection uses :func:`_lifecycle._in_sif_broker.is_in_sif` (added
in PR #261) so the two broker paths (SAC-from-SAC spawn + this
ACL decision) share the same in-SIF signal.
"""

from __future__ import annotations

__all__ = ["dispatch_acl_decision"]


def dispatch_acl_decision(
    decision: str,
    *,
    sender: str,
    target: str,
    note: str | None,
) -> dict:
    """Route an ACL decision to the right backend.

    ``decision`` is one of ``"unblock"`` / ``"block"`` / ``"grant"``
    (the last is an alias of unblock; the host listen route accepts
    it for back-compat).

    Returns the result dict the backend produced. Raises
    :class:`_state._acl_broker_client.AclBrokerError` (in-SIF) or
    ``ValueError`` (bare-host empties) on failure.
    """
    from .._lifecycle._in_sif_broker import is_in_sif

    if is_in_sif():
        from .._state._acl_broker_client import broker_acl_decision

        return broker_acl_decision(decision, sender=sender, target=target, note=note)

    # Bare-host path — write the host's state.db directly via the
    # local helpers. "unblock" and the legacy "grant" alias share
    # one write path (matches the server's
    # :func:`_listen._acl_routes.acl_grant` alias).
    from .._state.grant_flush import (
        block_and_clear_pending,
        unblock_and_clear_pending,
    )

    if decision == "block":
        return block_and_clear_pending(sender=sender, target=target, note=note)
    return unblock_and_clear_pending(sender=sender, target=target, note=note)
