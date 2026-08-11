#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Group authority read STRAIGHT FROM THE SPEC — configuration, not state.

Operator ruling 2026-08-12: **"states → PostgreSQL, configuration →
files under git"**, and, about the SQLite state layer, ``state.db という
ものは使ってはいけません`` / ``sqlite 使った瞬間負けだと思った方が良い
です``.

An agent's named groups are **configuration**. They are authored by a
human in ``spec.yaml``::

    metadata:
      labels:
        groups: [developer, infra, active]

Nothing at runtime discovers, negotiates, or mutates them. They are a
pure function of a file. Yet until this module they reached every
authority gate through a **cache in a per-host, per-agent SQLite file**:

    spec.yaml ──agent_start──▶ node_comms_policy (state.db) ──ACL──▶ gate
               (persist_acl_policy)          ↑
                                    the only thing a gate ever read

That cache is the entire bug surface. It is written once, on this host,
at ``agent_start``; every gate then trusts it. So authority became a
function of *where an agent had previously been started* rather than of
what its spec says:

* **Inside a container** ``$SCITEX_AGENT_CONTAINER_STATE_DB`` points at
  ``/state/<name>/state.db`` — a private overlay shard holding only what
  that one agent wrote, and no ``node_comms_policy`` row for anybody.
  Measured on scitex-compute-04, 2026-08-11, from inside this very
  container: ``resolve_group_names`` returned ``[]`` for *every* agent
  including itself, while the spec on the same filesystem, resolvable in
  the same process, said ``['active', 'developer', 'infra']``. Since
  ``host_exec``'s ``ELIGIBLE_GROUPS`` is ``{developer, researcher,
  privileged}``, the spec says ALLOW and the cache says DENY.
* **On a host the agent never ran on** there is likewise no row — which
  is how ``sac agents relocate … --to scitex-compute-03`` produced
  ``403 ACL deny`` on nine probes at once.

Reading the spec makes both impossible, because the spec is the same
file everywhere: it is bind-mounted into the container (hence
``$SCITEX_AGENT_CONTAINER_YAML_DIRS``, which is why
:func:`..config._resolve.resolve_config` already works in-container) and
carried between hosts as a file. Two contexts that disagree about a
file's contents is a filesystem bug; two contexts that disagree about a
cache of it is Tuesday.

Tri-state, deliberately
-----------------------
Every function here returns ``None`` for **"no spec is visible from this
process"**, which is emphatically NOT the same fact as "the spec exists
and names no groups" (an empty set / empty string). Collapsing the two
is the original sin this module refuses to repeat — it is what let a
missing row read as a legitimate "ungrouped" and silently deny an agent
that holds authority. ``None`` means *"I could not answer; ask the
fallback"*; an empty result means *"I answered, and the answer is none"*.

Precedence, and why the spec WINS
---------------------------------
When a spec IS visible it is authoritative and the DB row is not
consulted at all — not unioned with. Union would mean a stale row could
keep granting a group the operator has since deleted from the spec, i.e.
revocation would silently not take effect. Removing ``developer`` from a
spec must remove developer authority. That is only sound because the DB
column has exactly one non-spec-derived writer — there is none:
``record_comms_policy`` is reached solely via
:func:`.._lifecycle._spawn_gate.persist_acl_policy` (at ``agent_start``)
and ``sac agents refresh-acl``, and **both derive the groups from
``metadata.labels`` through this same pure resolver**. So the DB can only
ever hold a possibly-stale copy of what this module reads fresh.

This module does the file I/O; the *interpretation* of the labels is
unchanged and still lives in the pure resolver
(:mod:`._group_resolver`) — ``all_named_groups`` for the MULTI-value
authority set, ``group_from_labels`` for the single PRIMARY mesh bucket.
The two projections keep their existing, deliberately different
semantics (any-of vs first-of); this module only changes *where the
labels are read from*.

Failure posture
---------------
Never raises into an ACL path. A missing, unreadable, non-YAML, or
structurally surprising spec yields ``None`` (fall back to the DB — the
previous behaviour), never an exception and never a fabricated grant.
The only thing that can produce a non-``None`` answer here is a spec
that parsed into a mapping.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

__all__ = [
    "group_name_from_spec",
    "group_names_from_spec",
    "spec_labels_for",
]

_logger = logging.getLogger(__name__)


def _default_spec_path(name: str) -> str | None:
    """Resolve ``name`` to its FLEET ``spec.yaml`` path, or ``None``.

    Deliberately NOT :func:`.._resolve.resolve_config`. That resolver
    searches **project-local scope first** (``<repo>/.scitex/
    agent-container/agents/<name>/spec.yaml``), which is right for
    ``sac agents start`` — an operator standing in a repo means the
    repo's spec — and wrong for a permission check. An agent's workdir
    is a repository it edits; letting a file inside that repository
    decide the agent's own ACL groups would make self-elevation a
    ``git add`` away, and would hand any repo the agent clones a say in
    fleet authority.

    So authority reads only the two FLEET-scope sources:

      1. ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` — the operator-controlled
         search path, and the one that is injected into every container
         (pointing back at the host's real agents dir), which is what
         makes this work identically inside a SIF and on bare metal.
      2. the user-scope agents dir (``$SCITEX_DIR/agent-container/agents``).

    Returns the first hit, or ``None`` when neither holds the agent —
    the tri-state "no spec visible from here", not an error.
    """
    from ._resolve import _operator_env_dirs, _try_dir, _user_agents_dir

    try:
        candidates = list(_operator_env_dirs())
        candidates.append(_user_agents_dir())
        for base in candidates:
            hit = _try_dir(base, name)
            if hit:
                return hit
    except Exception:
        # An unreadable / unresolvable search dir means "cannot answer",
        # which the caller must treat as fall-back — never as an error to
        # propagate into a permission check.
        return None
    return None


def spec_labels_for(
    name: str,
    *,
    spec_path_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any] | None:
    """Return ``metadata.labels`` from ``name``'s spec, or ``None``.

    ``None`` means "no spec is visible from this process" — the caller
    must fall back to another source. A spec that exists but declares no
    labels yields ``{}`` (an answer, not an absence).

    ``spec_path_resolver`` is the injection seam used by the tests to
    point at a fixture tree without touching process env; it defaults to
    the standard spec search path (project scope → user scope →
    ``$SCITEX_AGENT_CONTAINER_YAML_DIRS``), which is exactly the path
    that already resolves specs correctly inside a container.

    Reads the YAML directly rather than going through ``load_config``:
    an authority answer must not depend on the whole spec validating.
    A spec with an unrelated schema error elsewhere in the file still has
    perfectly readable group labels, and stripping an agent's authority
    because of it would turn a cosmetic validation failure into a
    fleet-wide permission outage.
    """
    if not name:
        return None
    resolver = spec_path_resolver or _default_spec_path
    path = resolver(name)
    if not path:
        return None
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except Exception as exc:  # unreadable / malformed / not YAML
        _logger.warning(
            "group authority: spec for %r at %s is unreadable (%s); "
            "falling back to the persisted policy row",
            name,
            path,
            exc,
        )
        return None
    if not isinstance(doc, dict):
        return None
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    labels = metadata.get("labels")
    if labels is None:
        return {}
    if not isinstance(labels, dict):
        # e.g. ``labels: [a, b]`` — structurally wrong. Treat as "the
        # spec answered, and it names nothing", not as a hard error.
        return {}
    return labels


def group_names_from_spec(
    name: str,
    *,
    spec_path_resolver: Callable[[str], str | None] | None = None,
) -> frozenset[str] | None:
    """Return EVERY named group ``name``'s spec authors, or ``None``.

    The MULTI-value AUTHORITY projection — the set the
    developer/researcher/privileged gates ask membership questions of.
    Delegates the label interpretation to
    :func:`._group_resolver.all_named_groups`, so the singular
    ``labels.group`` string and every element of the plural
    ``labels.groups`` list are honoured exactly as before.

    ``None`` = no spec visible (fall back). ``frozenset()`` = the spec
    is visible and names no groups (a real, final answer: ungrouped).
    """
    labels = spec_labels_for(name, spec_path_resolver=spec_path_resolver)
    if labels is None:
        return None
    from ._group_resolver import all_named_groups

    return all_named_groups(labels)


def group_name_from_spec(
    name: str,
    *,
    spec_path_resolver: Callable[[str], str | None] | None = None,
) -> str | None:
    """Return the PRIMARY named group from ``name``'s spec, or ``None``.

    The single-bucket projection the default-ACL mesh resolves through
    (first-of, with role derivation), via
    :func:`._group_resolver.group_from_labels`. Kept distinct from
    :func:`group_names_from_spec` on purpose: an agent authored as
    ``groups: [solver]`` must stay OUT of the fleet mesh, and that
    isolation guarantee depends on the mesh reading one bucket rather
    than any-of the set.

    ``None`` = no spec visible (fall back). ``""`` = the spec is visible
    and the agent is ungrouped.
    """
    labels = spec_labels_for(name, spec_path_resolver=spec_path_resolver)
    if labels is None:
        return None
    from ._group_resolver import group_from_labels

    return group_from_labels(labels)
