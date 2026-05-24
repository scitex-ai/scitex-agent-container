"""Spawn-permission gate + lineage recording — wired into core
``agent_start`` so EVERY spawn path enforces the same ACL (ADR-0010
Rule B / Phase 2: ``起動経路 = 記録経路 = ACL経路`` collapsed to one path).

Before this gate lived in core ``agent_start``, only the
``sac listen`` ``POST /agents`` handler ran :func:`check_spawn` +
:func:`record_lineage`. The MCP ``agent_start`` tool (clew's spawn
surface) and the plain ``sac agents start`` CLI both bypass that
handler — they shell straight through to core ``agent_start`` — so an
agent-from-agent spawn was NOT ACL-gated and the ``lineage`` table was
split-brained: only the server path wrote it, while ``instances.spawned_by``
(a descriptive string) was written on every local start.

This module unifies the two. The core start codepath now:

  1. resolves the *caller* identity from the parent agent's
     ``SAC_NAME`` container env (``None`` / empty → admin / human-operator
     / lead path — always allowed);
  2. runs :func:`check_spawn` — root-only spawn under current policy;
     a denied spawn raises :class:`SpawnDeniedError` BEFORE the runtime
     is touched;
  3. on allow with a non-empty caller, records the ``caller → child``
     edge in the ``lineage`` table (idempotent; same-parent re-record is
     a no-op).

The same ``SAC_NAME`` already drives ``_instances._spawned_by()`` (the
``instances.spawned_by`` string), so after this gate the lineage table
and the spawned_by string are written from the same identity on the
same codepath — the split-brain is resolved.

Scope (ADR-0010 staged plan): this is **Phase 2 / Step 1 only** —
make ALL spawn paths go through ``check_spawn`` + write the lineage
row. The ``spec.acl`` schema (Step 2) and ``child ⊆ parent`` clamp
(Step 3) are separate follow-up PRs; ``check_spawn``'s current binary
root/child policy is kept verbatim.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["SpawnDeniedError", "enforce_spawn_gate", "resolve_spawn_caller"]


class SpawnDeniedError(RuntimeError):
    """Raised when an agent-from-agent spawn is rejected by the ACL.

    Current policy (``spawn_allowed``): only a *root* node (no parent
    in ``lineage``) may spawn. A child caller gets this error, carrying
    the explicit allow/deny reason for the operator log + caller.
    """


def resolve_spawn_caller() -> str | None:
    """Return the spawning agent's identity, or ``None`` for admin paths.

    When a PARENT agent shells out ``sac agents start <child>`` from
    inside its container, its env carries ``SAC_NAME`` (the parent's own
    name). A bare CLI / lead / human-operator launch has no ``SAC_NAME``
    — that is the administrative path and is always allowed to spawn.

    Reads through the sac env helper so either prefix
    (``SAC_NAME`` / ``SCITEX_AGENT_CONTAINER_NAME``) is honoured. An
    empty string is normalised to ``None`` (admin).
    """
    from .._env import getenv

    caller = getenv("NAME")
    return caller or None


def enforce_spawn_gate(
    child_name: str,
    *,
    caller: str | None = None,
    db_path: Path | None = None,
) -> str | None:
    """Gate a spawn of ``child_name`` and record its lineage edge.

    This is the single chokepoint every spawn path funnels through (via
    core ``agent_start``). It performs both halves of the unified spawn
    contract:

    * **ACL gate** — :func:`check_spawn` against the resolved caller.
      On deny, raises :class:`SpawnDeniedError` (the caller / CLI turns
      it into a non-zero exit) BEFORE any runtime work happens.
    * **Lineage record** — on allow with a non-empty caller, the
      ``caller → child_name`` edge is written to the ``lineage`` table
      via :func:`record_lineage` (idempotent on the same parent; a
      re-parent to a different caller raises ``ValueError`` upstream —
      surfaced as a :class:`SpawnDeniedError`).

    Args:
        child_name: The agent being started.
        caller: The spawning identity. ``None`` (default) resolves from
            the parent's ``SAC_NAME`` env via :func:`resolve_spawn_caller`.
            An explicit ``caller`` (e.g. the server handler's request
            ``caller`` field) overrides the env.
        db_path: Optional isolated state.db (tests). ``None`` → default.

    Returns the resolved caller (``None`` for the admin path), so a
    diagnostic / log line can attribute the spawn.

    Raises:
        SpawnDeniedError: when the spawn is not permitted by current
            policy, or when the lineage edge conflicts with an existing
            different parent.
    """
    from .._listen._acl import check_spawn
    from .._state.state_db_nodes import record_lineage

    if caller is None:
        caller = resolve_spawn_caller()

    decision, reason = check_spawn(caller=caller, db_path=db_path)
    if decision == "deny":
        raise SpawnDeniedError(reason or f"spawn of {child_name!r} denied")

    # ``caller=None`` is the admin / operator / lead path — the new agent
    # starts as a root, so NO lineage edge is recorded (recording one
    # would make every operator-launched agent a child of "").
    if caller:
        try:
            record_lineage(child=child_name, parent=caller, db_path=db_path)
        except ValueError as exc:
            # A child that switches parents is exactly the identity drift
            # the ACL is meant to prevent — reject loudly, never silently
            # re-parent.
            raise SpawnDeniedError(str(exc)) from exc

    return caller
