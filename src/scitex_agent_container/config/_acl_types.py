"""Phase-3 capsule-isolation policy dataclasses (ADR-0010 Step 2).

Per-spec ACL fields that model the operator-facing YAML surface for
the five clew capsule-isolation gaps:

  1. ``spec.comms.outbound.{siblings,parent}`` — sender-side per-spec
     deny that overrides the group-default ACL.
  2. ``spec.comms.inbound.{siblings,parent}`` — receiver-side deny.
  3. ``spec.comms.a2a.listen`` — operator-friendly alias for
     ``spec.a2a.port: null`` (suppress inbound sidecar entirely).
  4. ``spec.lineage.group: solitary`` — force this agent's default
     ACL group to ``{name}``; no transitive parent-group inheritance.
  5. ``spec.lineage.may_spawn: false`` — per-spec spawn deny that
     survives global-policy relaxation.

These are PURE declarations. Server-side enforcement reads the
persisted values from the ``node_comms_policy`` table at ACL-check
time (see :mod:`scitex_agent_container._listen._acl` and
:mod:`scitex_agent_container._state.state_db_nodes`).

Defaults intentionally preserve pre-Phase-3 behaviour (everything
``"allow"`` / ``True`` / ``""``), so a spec that omits both
``spec.comms`` and ``spec.lineage`` behaves byte-identically.

Lives in a sibling module (not in ``_types.py``) to keep that
module under the per-file line cap; ``_types.py`` re-imports the
classes for the :class:`AgentConfig` field defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "A2ACommsToggle",
    "CommsSpec",
    "InboundCommsSpec",
    "LineageSpec",
    "OutboundCommsSpec",
]


@dataclass
class OutboundCommsSpec:
    """Outbound A2A policy: which relationships THIS agent may address.

    ``"allow"`` (default) keeps the pre-Phase-3 group-default ACL
    (intra-group bidirectional). ``"deny"`` makes the agent refuse to
    SEND to that relationship even when group membership would
    normally permit it. Evaluated sender-side against the resolved
    sender→target lineage relationship.
    """

    siblings: str = "allow"  # "allow" | "deny"
    parent: str = "allow"


@dataclass
class InboundCommsSpec:
    """Inbound A2A policy: which relationships may address THIS agent.

    Mirror of :class:`OutboundCommsSpec` but enforced on the receiver
    side. A ``"deny"`` here returns a 403 to a sibling/parent sender
    even if the sender's own outbound policy permits it.
    """

    siblings: str = "allow"
    parent: str = "allow"


@dataclass
class A2ACommsToggle:
    """Suppress the inbound A2A listen sidecar entirely.

    Operator-friendly alias for ``spec.a2a.port: null``. When
    ``listen=False`` the loader propagates the disable into the
    :class:`A2ASpec` (sets ``port=None``); the runner's
    ``serve_inbound`` is then never launched for this agent. Defaults
    to ``True`` so absence preserves current behaviour.
    """

    listen: bool = True


@dataclass
class CommsSpec:
    """Top-level ``spec.comms`` block: outbound / inbound / a2a."""

    outbound: OutboundCommsSpec = field(default_factory=OutboundCommsSpec)
    inbound: InboundCommsSpec = field(default_factory=InboundCommsSpec)
    a2a: A2ACommsToggle = field(default_factory=A2ACommsToggle)


@dataclass
class LineageSpec:
    """Per-spec lineage policy.

    * ``group`` — when ``"solitary"``, the agent's default-ACL group
      is forced to ``{name}`` regardless of what the runtime lineage
      table would otherwise infer. No transitive parent-group
      inheritance — clew capsule children that should NOT see their
      parent's other capsules set ``group: solitary``. Empty string
      (default) keeps the existing lineage-table-derived group.
    * ``may_spawn`` — when ``False``, this agent is forbidden from
      calling ``sac agents start`` even after the global root-only
      policy would have allowed it. Evaluated AFTER
      :func:`spawn_allowed`'s global check so a future relaxation of
      the global policy never silently re-enables a per-spec deny.

    Defaults preserve current behaviour: ``group=""`` ⇒ derive from
    lineage; ``may_spawn=True`` ⇒ deferred entirely to global policy.
    """

    group: str = ""  # "" | "solitary"
    may_spawn: bool = True
