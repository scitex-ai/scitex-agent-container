"""Which agents get the host ``~/.claude`` deep-merge — the classification gate.

Extracted from :mod:`._host_merge` (which was over the line cap) because this
is a POLICY decision with two independent consumers, not merge mechanics:

  * :mod:`._host_merge` — whether to deep-merge the host ``~/.claude`` on top
    of the ``_shared`` / per-agent ``to_home`` layers
  * :mod:`..cli_pkg._explain` — whether ``sac agents explain`` prints the
    "Settings sources (settings.json key -> to_home layer)" provenance section

That second consumer is why a wrong answer here is expensive out of proportion
to its size: when the gate says False, the ONE tool that can answer "where did
this hook come from" silently prints ``off`` instead of the provenance it has
already computed.

THE BUG THIS MODULE WAS EXTRACTED TO FIX, measured 2026-08-03 across all 102
fleet specs::

    labels.group   (SINGULAR — what the gate read)   ->   0 specs
    labels.groups  (PLURAL   — what specs write)     ->  86 specs

The scalar branch had NEVER FIRED. Every agent was classified by the fallback
alone — i.e. by whether its role STRING sat on a four-item allowlist. So
``groups: [developer]``, the field the specs and the fleet registry both use,
had no effect on this gate at all.

Concrete cost: scitex-hub declares ``groups: ["developer"]`` with role
``product-lead-orchestrator``. When the operator elevated it from maintainer to
lead on 2026-07-17, that rename moved it off the allowlist and therefore out of
the host deep-merge — silently, with no warning, and it also switched off the
provenance display. Two agents then spent an evening searching six locations
for a hook registration while the tool that would have shown it was disabled
for that agent.

Honouring ``groups`` flips exactly FIVE agents (dry-run over all 102 specs
driving this function): scitex-cards-chat, scitex-cards-gui,
scitex-cards-mobile, scitex-hub, spartan-dev. The other 80 developers already
pass via the role fallback, so this is a five-agent change, not a fleet-wide
one — a distinction I got wrong by 17x before measuring it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AgentConfig

#: Role strings that imply full-developer WHEN NO GROUP IS DECLARED. This is a
#: FALLBACK, deliberately narrow, and it is the reason a role rename can change
#: an agent's config layers — see the module docstring. Prefer declaring
#: ``groups`` explicitly over relying on it.
_DEVELOPER_ROLES: frozenset[str] = frozenset(
    {"project-maintainer", "maintainer", "dev-agent", "contributor"}
)

__all__ = ["is_full_developer", "_DEVELOPER_ROLES"]


def is_full_developer(config: "AgentConfig") -> bool:
    """True iff ``config`` is a FULL-DEVELOPER agent (host deep-merge ON).

    Resolution order:

      1. ``metadata.labels.groups`` (LIST) — GRANTS when it contains
         ``developer``. Additive only: it never refuses.
      2. ``metadata.labels.group`` (SCALAR) — the historical spelling. Grants on
         ``developer``, REFUSES on any other non-empty value (e.g. ``solitary``).
      3. ``metadata.labels.role`` in :data:`_DEVELOPER_ROLES` — fallback, used
         when neither spelling decided.

    WHY (1) IS ADDITIVE AND (2) IS NOT, since the asymmetry is deliberate and
    looks like an oversight: making the plural form refuse as well is more
    principled, and it REVOKES the host deep-merge from SEVEN agents that hold
    it today (claude-code-telegrammer, neurovista, neurovista-paper-writer,
    paper-ripple-wm, sales-worker and two templates) — they declare a
    non-developer ``groups`` while carrying a role on the allowlist. Measured,
    not guessed: a full old-vs-new diff over 102 specs in separate interpreters.

    Silently stripping config layers from seven working agents as a SIDE EFFECT
    of fixing a field name is exactly the class of change this gate's own bug
    already caused once. Refusal semantics for ``groups`` deserve their own
    deliberate decision and their own migration, not a rider on this fix.
    """
    labels = getattr(config, "labels", None) or {}

    raw_groups = labels.get("groups")
    if raw_groups is not None:
        if not isinstance(raw_groups, (list, tuple, set)):
            raw_groups = [raw_groups]
        groups = {str(g).strip().lower() for g in raw_groups if str(g).strip()}
        # ADDITIVE ONLY -- it grants, it never refuses. See the module docstring
        # for why: making an explicit `groups` authoritative in both directions
        # is defensible in the abstract and REVOKES the host merge from seven
        # live agents that hold it today.
        if "developer" in groups:
            return True

    group = str(labels.get("group", "") or "").strip().lower()
    if group == "developer":
        return True
    if group:  # any explicit non-developer group (e.g. "solitary") -> no merge
        return False

    role = str(labels.get("role", "") or "").strip().lower()
    return role in _DEVELOPER_ROLES
