"""NAMED-GROUP membership — the second grouping axis, and its provenance.

Extracted from :mod:`.state_db_nodes` (587 lines, over the 512 budget) as one
cohesive responsibility. Named groups are explicitly "a SECOND grouping axis
layered on top of the lineage-derived group mesh", which makes them separable
from lineage recording, spawn authorisation and host resolution.

Two resolvers, and the difference between them is the point of this module:

* :func:`resolve_group_name` returns a bare string. Correct for MEMBERSHIP
  TESTS, which genuinely do not care why a group is absent.
* :func:`resolve_group` returns :class:`GroupResolution` — the group AND the
  provenance of that answer. Correct for anything a HUMAN will read.

WHY THE SECOND ONE EXISTS. ``resolve_group_name`` returns ``""`` for three
different situations, and an ACL denial built on it said only::

    caller 'alice' resolves to group ''

which does not tell an operator whether to label the agent or to go looking
for the right database. The states need different actions:

    ungrouped       a policy row exists, group_name empty  -> label the agent
    no_policy_row   no row at all                          -> check the DB path
    no_caller       nothing was looked up

The collapse originates one layer down and is deliberate there:
``read_comms_policy`` documents that "a missing row yields
DEFAULT_COMMS_POLICY so the 'no-row' vs 'row-with-default-values'
distinction is invisible to callers". That is right for every caller that
wants defaults — and is exactly what has to be undone when the answer is
going to be shown to a person. So :func:`resolve_group` queries the table
directly rather than reaching through that helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .state_db_acl_policy import read_comms_policy

#: How a group resolution came about. FOUR states, deliberately, because they
#: call for different operator actions and a bare ``""`` made them one.
GROUP_SOURCES = ("named", "ungrouped", "no_policy_row", "no_caller")


@dataclass(frozen=True)
class GroupResolution:
    """A named group PLUS why it came out that way.

    ``group_name`` is non-empty only when ``source == "named"``; the
    validator enforces that so a malformed answer fails where it is built
    rather than three layers downstream.
    """

    group_name: str
    source: str

    def __post_init__(self) -> None:
        if self.source not in GROUP_SOURCES:
            raise ValueError(
                f"unknown group source {self.source!r}; expected one of "
                f"{GROUP_SOURCES}"
            )
        if self.group_name and self.source != "named":
            raise ValueError(
                f"source {self.source!r} must carry an empty group_name, got "
                f"{self.group_name!r}"
            )
        if not self.group_name and self.source == "named":
            raise ValueError("source 'named' requires a non-empty group_name")

    @property
    def is_named(self) -> bool:
        """True iff the agent actually has a named group."""
        return self.source == "named"

    def describe(self) -> str:
        """One clause naming the state AND what to do about it.

        Written for an ACL denial message, where "an error that only states
        what broke is half-written".
        """
        if self.source == "named":
            return f"group {self.group_name!r}"
        if self.source == "ungrouped":
            return (
                "no named group (a comms-policy row exists, but its "
                "group_name is empty) — set `metadata.labels.group` in the "
                "agent's spec and restart it so the group is persisted"
            )
        if self.source == "no_policy_row":
            return (
                "NO comms-policy row at all — the agent was never started "
                "via `agent_start`, or it was started against a DIFFERENT "
                "state db than the one being read; verify the db path before "
                "treating this as a grouping problem"
            )
        return "no caller name was supplied, so no group was looked up"


def resolve_group(
    *,
    name: str,
    db_path: Path | None = None,
) -> GroupResolution:
    """Resolve ``name``'s named group AND the provenance of that answer.

    Queries ``node_comms_policy`` directly rather than through
    :func:`read_comms_policy`, whose documented contract is to hide the
    "no row" vs "row with defaults" distinction. Hiding it is correct
    there and wrong here.
    """
    if not name:
        return GroupResolution(group_name="", source="no_caller")
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT group_name FROM node_comms_policy WHERE name = ?",
            (name,),
        ).fetchone()
    if row is None:
        return GroupResolution(group_name="", source="no_policy_row")
    group_name = str(row["group_name"] or "")
    if not group_name:
        return GroupResolution(group_name="", source="ungrouped")
    return GroupResolution(group_name=group_name, source="named")


def resolve_group_name(
    *,
    name: str,
    db_path: Path | None = None,
) -> str:
    """Return ``name``'s persisted NAMED group, or ``""`` if ungrouped.

    Reads ``node_comms_policy.group_name`` (written at ``agent_start``
    from the resolved ``metadata.labels.group`` / role default). An agent
    with no policy row, or a row with an empty ``group_name``, is
    "ungrouped" and shares a named group with no one.

    UNCHANGED ON PURPOSE. Membership comparisons do not care which flavour
    of "ungrouped" this is, and widening the return type would churn every
    call site for no benefit. When the answer will be shown to a HUMAN,
    call :func:`resolve_group` instead.
    """
    if not name:
        return ""
    policy = read_comms_policy(name=name, db_path=db_path)
    return str(policy.get("group_name", "") or "")


def same_named_group(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff ``sender`` and ``target`` share a NAMED group.

    Both groups must be NON-EMPTY and equal. Two ungrouped agents (empty
    group) do NOT match — that keeps absence byte-equivalent to the
    pre-group-name behaviour (an ungrouped fleet falls through to the
    lineage-mesh + explicit-grant ACL exactly as before).
    """
    sender_group = resolve_group_name(name=sender, db_path=db_path)
    if not sender_group:
        return False
    target_group = resolve_group_name(name=target, db_path=db_path)
    return target_group == sender_group


def is_developer(
    *,
    name: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff ``name``'s resolved NAMED group is ``developer``.

    The developer group has FULL AUTHORITY (operator 2026-06-25): members
    may CRUD agents (spawn / start / stop / restart / delete) and CRUD the
    ACL (grant / revoke). The spawn + lineage ACL gates consult this to
    short-circuit their default (root-only / lineage-descendant) checks.
    """
    from ..config._group_resolver import is_developer_group

    return is_developer_group(resolve_group_name(name=name, db_path=db_path))


def is_researcher(
    *,
    name: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff ``name``'s resolved NAMED group is ``researcher``.

    Mirrors :func:`is_developer` for the research-role group
    (:data:`scitex_agent_container.config._group_resolver.RESEARCHER_GROUP`).
    Per the operator's 2026-07-05 ruling ("dev agents and research agents
    MUST have full permissions — including the ability to start/stop peer
    agents"), a researcher-group member gets the same spawn authority as a
    developer-group member; see :func:`spawn_allowed`.
    """
    from ..config._group_resolver import RESEARCHER_GROUP

    group = resolve_group_name(name=name, db_path=db_path)
    if not group:
        return False
    return group.strip().lower() == RESEARCHER_GROUP.lower()


__all__ = [
    "GROUP_SOURCES",
    "GroupResolution",
    "is_developer",
    "is_researcher",
    "resolve_group",
    "resolve_group_name",
    "same_named_group",
]
