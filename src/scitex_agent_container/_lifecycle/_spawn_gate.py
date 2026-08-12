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
  2. runs :func:`check_spawn`; a denied spawn raises
     :class:`SpawnDeniedError` BEFORE the runtime is touched;
  3. on allow with a non-empty caller, records the ``caller → child``
     edge in the ``lineage`` table (idempotent; same-parent re-record is
     a no-op).

The same ``SAC_NAME`` already drives ``_instances._spawned_by()`` (the
``instances.spawned_by`` string), so after this gate the lineage table
and the spawned_by string are written from the same identity on the
same codepath — the split-brain is resolved.

Current spawn policy — NOT root-only (this prose said "root-only" long
after the code stopped being root-only, and that stale sentence is how
the policy got mis-triaged; keep it in step with
:func:`._listen._acl.check_spawn`, which is the SSOT):

  * admin / operator (no ``SAC_NAME``) — allowed;
  * a caller whose named groups INCLUDE ``developer``, ``researcher`` or
    ``privileged`` — allowed EVEN AS A CHILD (operator ruling: dev and
    research agents must both be able to start/stop peer agents;
    ``privileged`` joined them 2026-07-16). Membership, not primary-group
    equality — ``groups: [generalist, developer]`` IS a developer;
  * a ROOT node (no lineage parent) — allowed;
  * any other child (``generalist`` / isolated solver / ungrouped) —
    denied, as is any caller with ``spec.lineage.may_spawn=false``.

Scope (ADR-0010 staged plan): this module is **Phase 2 / Step 1** —
make ALL spawn paths go through ``check_spawn`` + write the lineage
row. The ``spec.acl`` schema (Step 2) and ``child ⊆ parent`` clamp
(Step 3) are separate follow-up PRs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "SpawnDeniedError",
    "enforce_spawn_gate",
    "persist_acl_policy",
    "resolve_spawn_caller",
]


def persist_acl_policy(config: Any, db_path: Path | None = None) -> None:
    """Write the loaded spec's Phase-3 ACL policy into ``node_comms_policy``.

    Idempotent upsert keyed by ``config.name``. Called from core
    ``agent_start`` after :func:`enforce_spawn_gate` runs so a re-start
    after a spec edit re-publishes the policy. ``config`` is the
    :class:`scitex_agent_container.config._types.AgentConfig` produced
    by ``load_config`` — only ``name``, ``comms``, and ``lineage`` are
    touched. ``AgentProxy`` kinds (no SDK, no inbound) are written too
    so ``read_comms_policy`` always finds a row for any started agent.
    """
    from .._state.state_db_nodes import record_comms_policy
    from ..config._group_resolver import all_named_groups, group_from_labels

    comms = config.comms
    lineage = config.lineage
    labels = getattr(config, "labels", None)
    # TWO projections of the SAME metadata.labels, written together so
    # they cannot drift (incident 2026-08-10):
    #   group_name  — the PRIMARY group (labels.group, else the first
    #                 element of labels.groups, else role-derived). The
    #                 default-ACL mesh resolves through this single
    #                 bucket, which keeps an isolated solver isolated.
    #   group_names — EVERY group the labels name. The AUTHORITY gates
    #                 (developer / researcher / privileged) read this,
    #                 so naming a group anywhere in the list grants it
    #                 and list ORDER stops deciding permissions.
    group_name = group_from_labels(labels)
    group_names = all_named_groups(labels)
    record_comms_policy(
        name=config.name,
        outbound_siblings=comms.outbound.siblings,
        outbound_parent=comms.outbound.parent,
        inbound_siblings=comms.inbound.siblings,
        inbound_parent=comms.inbound.parent,
        lineage_group=lineage.group,
        may_spawn=lineage.may_spawn,
        group_name=group_name,
        group_names=group_names,
        db_path=db_path,
    )


class SpawnDeniedError(RuntimeError):
    """Raised when an agent-from-agent spawn is rejected by the ACL.

    Carries the explicit deny reason from :func:`._listen._acl.check_spawn`
    (the policy SSOT) for the operator log + the caller. A ROOT node, and a
    ``developer``- or ``researcher``-group caller, may spawn; any other
    child — and any caller with ``spec.lineage.may_spawn=false`` — gets
    this error. (This docstring used to say "only a root node may spawn",
    which the code had already outgrown.)
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
            policy. (A re-parent attempt no longer raises — record_lineage
            keeps the existing parent in-place, so restarts are allowed.)
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
            # record_lineage keeps the existing parent on a re-parent
            # attempt (restart-in-place) rather than raising; this except
            # now only guards the empty child/parent programming error.
            raise SpawnDeniedError(str(exc)) from exc

    return caller
