"""MULTI-value named-group readers — the AUTHORITY half of the group ACL.

Lives in a sibling module so :mod:`.state_db_nodes` stays under the
per-file line cap. Re-exported from ``state_db_nodes`` so the natural
import path keeps working::

    from scitex_agent_container._state.state_db_nodes import (
        is_developer, is_privileged, is_researcher, resolve_group_names,
    )

Why this module exists (incident 2026-08-10, ``grant``)
-------------------------------------------------------
An agent's spec authors its groups as a LIST::

    metadata:
      labels:
        groups: [generalist, privileged, developer, researcher, active]

Two readers consumed that list and disagreed, with no error anywhere:

* ``a2a_peers`` reported all five, via the MULTI-value
  :func:`scitex_agent_container.config._group_resolver.all_named_groups`.
* The ACL saw exactly ONE — ``group_from_labels`` keeps the FIRST
  element, that single string was persisted into
  ``node_comms_policy.group_name``, and every authority gate
  (``is_developer`` / ``is_researcher`` / the ``privileged`` fallthrough
  in ``spawn_allowed`` / ``host_exec``'s ``ELIGIBLE_GROUPS``) compared
  against it.

So ``grant``'s spawn was refused with "is in none of the developer,
research, or privileged groups" while its registry row listed three of
them. The agent's authority depended on the ORDER of a YAML list —
moving ``developer`` to the front would have silently fixed it, which is
the clearest possible sign the reduction was wrong.

The cut this module draws
-------------------------
* **Authority is MEMBERSHIP — any-of.** "May this agent spawn / manage /
  host-exec" is a capability the operator grants by naming the group
  ANYWHERE in ``labels.groups``. That is what the functions here answer,
  over the FULL set.
* **The default-ACL mesh keeps ONE bucket per agent — first-of.** "Do
  these two agents coordinate by default" still resolves through
  :func:`.state_db_nodes.resolve_group_name` (the primary group),
  deliberately unchanged: a solver authored as ``groups: [solver]`` must
  stay outside the fleet mesh, and collapsing the two questions is how
  that isolation guarantee would erode.

Both projections are now written from the SAME ``metadata.labels`` at
``agent_start`` (:func:`.._lifecycle._spawn_gate.persist_acl_policy`),
so the persisted set is lossless and the two readers cannot disagree
about what the spec said.
"""

from __future__ import annotations

# ``db_path`` IS GONE from every reader below (2026-08-28). It named a
# local database file, and the only thing any of them did with it was hand
# it to ``read_comms_policy`` — which now reads ``node_comms_policy`` from
# PostgreSQL. Leaving the parameter would have been worse than removing
# it: a caller passing a ``tmp_path`` would believe it had isolated the
# read, and it would silently have been reading the live fleet store.

__all__ = [
    "in_named_group",
    "is_developer",
    "is_privileged",
    "is_researcher",
    "resolve_group_name",
    "resolve_group_names",
    "same_named_group",
]


def resolve_group_name(
    *,
    name: str,
) -> str:
    """Return ``name``'s PRIMARY named group, or ``""`` if ungrouped.

    The single-bucket projection the default-ACL mesh resolves through
    (first-of + role derivation), as opposed to the any-of authority set
    :func:`resolve_group_names` returns. The distinction is load-bearing:
    a solver authored as ``groups: [solver]`` must stay outside the fleet
    mesh.

    Reads the SPEC first, for the same reasons and with the same
    tri-state contract as :func:`resolve_group_names` — the two
    projections must resolve through one source or they start
    disagreeing about the same agent, which is precisely the
    2026-08-10 ``grant`` incident. Falls back to
    ``node_comms_policy.group_name`` only when no spec is visible from
    this process (foreign / remote nodes).
    """
    if not name:
        return ""
    from ..config._group_authority import group_name_from_spec

    from_spec = group_name_from_spec(name)
    if from_spec is not None:
        return from_spec
    from .state_db_acl_policy import read_comms_policy

    policy = read_comms_policy(name=name)
    return str(policy.get("group_name", "") or "")


def same_named_group(
    *,
    sender: str,
    target: str,
) -> bool:
    """Return ``True`` iff ``sender`` and ``target`` share a NAMED group.

    Both groups must be NON-EMPTY and equal. Two ungrouped agents
    (empty group) do NOT match — that keeps absence byte-equivalent to
    the pre-group-name behaviour (an ungrouped fleet falls through to
    the lineage-mesh + explicit-grant ACL exactly as before).
    """
    sender_group = resolve_group_name(name=sender)
    if not sender_group:
        return False
    target_group = resolve_group_name(name=target)
    return target_group == sender_group


def resolve_group_names(
    *,
    name: str,
) -> frozenset[str]:
    """Return EVERY named group ``name``'s persisted policy row holds.

    Reads ``node_comms_policy.group_names`` (the full authored set) and
    UNIONS it with ``node_comms_policy.group_name`` (the primary). The
    union is deliberate and load-bearing in two directions:

    * A row written BEFORE the ``group_names`` column existed has an
      empty set and a non-empty primary, so it still resolves to
      ``{primary}`` — byte-equivalent to the pre-multi-group behaviour.
      No backfill is required for correctness; ``sac agents refresh-acl``
      (or the agent's next start) upgrades the row to the full set.
    * The two columns can therefore never disagree in the direction that
      REMOVES authority, which is the failure mode this whole module
      exists to make impossible.

    Group names are returned verbatim (whitespace already trimmed at
    write time); compare through :func:`in_named_group`, which folds
    case. An unknown / empty name yields the empty set.

    **The spec answers first** (operator 2026-08-12, "configuration →
    files under git"). Named groups are configuration: a human writes
    them in ``spec.yaml`` and nothing at runtime changes them. When that
    file is visible from this process it IS the answer, and the DB row
    is not consulted — see
    :mod:`scitex_agent_container.config._group_authority` for the full
    argument. The persisted row remains the fallback for names with no
    visible spec (foreign / remote nodes registered through
    ``comms_nodes``, and any host that has the row but not the file).

    This is what makes the answer identical in a container and on the
    bare host. The DB could not do that: in a SIF,
    ``$SCITEX_AGENT_CONTAINER_STATE_DB`` is a private per-agent shard
    holding no policy row for anyone, so this function returned the
    empty set for EVERY agent — measured on scitex-compute-04
    2026-08-11 — while the spec on the same filesystem said
    ``['active', 'developer', 'infra']``. Empty means "denied" at every
    authority gate, so the cache turned a readable fact into a 403.

    The spec's answer REPLACES the row rather than unioning with it, so
    deleting a group from a spec actually revokes it. That is sound only
    because no writer puts non-spec-derived groups in the column:
    ``record_comms_policy`` is reached solely from ``persist_acl_policy``
    (at ``agent_start``) and ``sac agents refresh-acl``, and both derive
    the value from ``metadata.labels`` through the same pure resolver.
    """
    if not name:
        return frozenset()
    from ..config._group_authority import group_names_from_spec

    # Tri-state: None = no spec visible here (fall through to the row);
    # frozenset() = the spec IS visible and names no groups, which is a
    # final answer, not an absence.
    from_spec = group_names_from_spec(name)
    if from_spec is not None:
        return from_spec
    from .state_db_acl_policy import read_comms_policy

    policy = read_comms_policy(name=name)
    out = {str(g).strip() for g in policy.get("group_names", ()) if str(g).strip()}
    primary = str(policy.get("group_name", "") or "").strip()
    if primary:
        out.add(primary)
    return frozenset(out)


def in_named_group(
    *,
    name: str,
    group: str,
) -> bool:
    """Return ``True`` iff ``name``'s persisted groups include ``group``.

    Case-insensitive, whitespace-trimmed on both sides — the same
    comparison the pure predicates in
    :mod:`scitex_agent_container.config._group_resolver` apply, lifted
    from one group to the whole set.
    """
    wanted = str(group).strip().lower()
    if not wanted:
        return False
    return any(
        g.strip().lower() == wanted
        for g in resolve_group_names(name=name)
    )


def is_developer(
    *,
    name: str,
) -> bool:
    """Return ``True`` iff ``developer`` is among ``name``'s named groups.

    The developer group has FULL AUTHORITY (operator 2026-06-25):
    members may CRUD agents (spawn / start / stop / restart / delete)
    and CRUD the ACL (grant / revoke). The spawn + lineage ACL gates
    consult this to short-circuit their default (root-only / lineage-
    descendant) checks.

    MEMBERSHIP, not primary-group equality (incident 2026-08-10): an
    agent whose spec says ``groups: [generalist, developer]`` is a
    developer. It previously was not, because only the first element
    reached the DB.
    """
    from ..config._group_resolver import DEVELOPER_GROUP

    return in_named_group(name=name, group=DEVELOPER_GROUP)


def is_researcher(
    *,
    name: str,
) -> bool:
    """Return ``True`` iff ``researcher`` is among ``name``'s named groups.

    Mirrors :func:`is_developer` for the research-role group
    (:data:`scitex_agent_container.config._group_resolver.RESEARCHER_GROUP`).
    Per the operator's 2026-07-05 ruling ("dev agents and research
    agents MUST have full permissions — including the ability to
    start/stop peer agents"), a researcher-group member gets the same
    spawn authority as a developer-group member; see
    :func:`.state_db_nodes.spawn_allowed`.
    """
    from ..config._group_resolver import RESEARCHER_GROUP

    return in_named_group(name=name, group=RESEARCHER_GROUP)


def is_privileged(
    *,
    name: str,
) -> bool:
    """Return ``True`` iff ``privileged`` is among ``name``'s named groups.

    Completes the trio of spawn-authorised groups (operator ruling
    2026-07-16: denying a privileged-group agent — dotfiles — "is a sac
    bug"). Same membership semantics as :func:`is_developer`.
    """
    from ..config._group_resolver import PRIVILEGED_GROUP

    return in_named_group(name=name, group=PRIVILEGED_GROUP)
