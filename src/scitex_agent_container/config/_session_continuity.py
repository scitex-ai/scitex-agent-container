"""Session-continuity resolution — ``continue`` by default, opt-in ``fresh``.

Single source of truth for two related decisions:

  * :func:`role_wants_continuity` — given an agent's role string, should a
    spec that OMITS ``claude.session`` default to ``continue`` (keep the
    prior conversation) instead of the global ``fresh`` default? Long-lived
    *coordinator* roles (lead / head / worker / telegrammer /
    project-maintainer / …) answer ``True``; experiment-capsule agents —
    which carry no coordinator role at all — answer ``False`` and stay
    ``fresh``. Applied in :mod:`config._loaders` right after
    :func:`config._parsers._claude.parse_claude`, where the
    ``metadata.labels.role`` / ``spec.env.SCITEX_AGENT_CONTAINER_ROLE`` role
    is available (the claude parser only sees the ``spec.claude`` block).

  * :func:`wants_continue` — given a fully-resolved ``claude.session``
    value (after the loader role-default and any CLI override), should the
    launcher resume the prior session? ``continue`` → ``True``; ``fresh`` /
    ``resume`` → ``False`` (``resume`` drives ``--resume <id>`` separately,
    not bare ``-c``). The interactive-TUI argv builder
    (``runtimes/_apptainer_inner_argv._tui_runner_argv``) calls this to
    decide whether to append ``claude -c``.

Why a flip + role-default rather than just emitting ``session: continue``
into every coordinator spec: the coordinator specs are hand-deployed
OUTSIDE this repo (under ``~/.scitex/agent-container/agents/`` and the
dotfiles mirror) and none of them currently set ``claude.session``. A
role-keyed default keeps them continuous on the next start WITHOUT touching
each spec, while every experiment capsule (no coordinator role) gets a
hermetic fresh session. An operator can always override per-spec
(``session: fresh`` on a coordinator, ``session: continue`` on a one-off)
or per-start (``sac start --fresh`` / ``--continue``).
"""

from __future__ import annotations

# Canonical resolved session-continuity modes. ``parse_claude`` normalises
# the legacy ``new-session`` / ``new`` aliases onto ``fresh`` before any of
# this runs, so only these three reach the resolver.
SESSION_FRESH = "fresh"
SESSION_CONTINUE = "continue"
SESSION_RESUME = "resume"

# Roles whose agents are LONG-LIVED coordinators: they must keep their
# working memory across restarts, so a spec that omits ``claude.session``
# defaults them to ``continue`` instead of the global ``fresh``.
#
# Two role surfaces feed this (see :func:`role_wants_continuity`):
#   * ``metadata.labels.role`` — the spec-authored role
#     (``project-maintainer`` / ``quality-agent`` / ``dev-agent`` / …).
#   * ``spec.env.SCITEX_AGENT_CONTAINER_ROLE`` — the fleet role injected by
#     scitex-agent-container's auto-generated CLAUDE.md block
#     (``lead`` / ``head`` / ``worker`` / ``telegrammer`` /
#     ``worker-telegrammer`` / …).
#
# Match is case-insensitive on the exact role OR any ``_CONTINUITY_PREFIXES``
# prefix (roles are frequently project-suffixed, e.g.
# ``worker-telegrammer-nas``, ``contributor-figrecipe``).
_CONTINUITY_ROLES: frozenset[str] = frozenset(
    {
        # Fleet coordinator roles (env-injected).
        "lead",
        "head",
        "worker",
        "telegrammer",
        "worker-telegrammer",
        "coordinator",
        "orchestrator",
        # Spec-authored long-lived roles.
        "project-maintainer",
        "maintainer",
        "quality-agent",
        "dev-agent",
        "contributor",
    }
)

# Role PREFIXES that mark a continuity role even when project-suffixed.
# e.g. ``worker-telegrammer-nas`` / ``contributor-figrecipe`` /
# ``lead-ywata-note-win`` / ``head-ywata-note-win``.
_CONTINUITY_PREFIXES: tuple[str, ...] = (
    "lead-",
    "lead_",
    "head-",
    "head_",
    "worker-",
    "worker_",
    "telegrammer-",
    "telegrammer_",
    "contributor-",
    "coordinator-",
    "orchestrator-",
)


def role_wants_continuity(role: str | None) -> bool:
    """Return True iff ``role`` is a long-lived coordinator role.

    A spec that OMITS ``claude.session`` is defaulted to ``continue`` when
    this returns True (else it keeps the global ``fresh`` default). Empty /
    ``None`` role (e.g. an experiment capsule with no role label) → False.
    Matching is case-insensitive on the exact role name or any
    :data:`_CONTINUITY_PREFIXES` prefix.
    """
    if not role:
        return False
    norm = str(role).strip().lower()
    if not norm:
        return False
    if norm in _CONTINUITY_ROLES:
        return True
    return norm.startswith(_CONTINUITY_PREFIXES)


def default_session_for_role(role: str | None) -> str:
    """Session mode for a spec that OMITTED ``claude.session``: ``continue``.

    CONTINUE FOR EVERY ROLE, INCLUDING NONE. Operator ruling 2026-08-18:
    「フレッシュは基本的に使いません、最初の起動に必要な時だけで ... スペックは
    全てレジュームで」 — fresh is essentially never used; only where a FIRST
    boot needs it; every spec resumes.

    This inverts the previous polarity and that is the whole point. The old
    rule was an ALLOWLIST: a role had to appear in ``_CONTINUITY_ROLES`` or
    match a prefix to keep its memory, and anything unenumerated silently got
    ``fresh``. Measured 2026-08-18, that is not hypothetical — scitex-hub's
    role is ``product-lead-orchestrator``, which matches neither the exact set
    nor any prefix (it begins ``product-``, not ``lead-``), so it resolved to
    ``fresh`` and lost a day of working memory on restart. 91 of 117 live
    specs omit ``claude.session`` entirely, so the default decided the
    fleet's memory and nobody had chosen it.

    A capsule that genuinely wants a hermetic session is UNAFFECTED in
    practice: with no prior session on disk there is nothing to continue, so
    the first boot is fresh either way. What changes is the SECOND boot, and
    that is exactly the case the operator is protecting.

    ``role`` is retained in the signature (callers pass it) and
    :func:`role_wants_continuity` is kept for callers that still ask the
    role question directly, but the role no longer GATES the default.
    """
    return SESSION_CONTINUE


def wants_continue(session: str | None) -> bool:
    """Return True iff a resolved session mode means "resume prior session".

    ``continue`` → True. ``fresh`` (and its normalised aliases) → False.
    ``resume`` → False here: it is delivered as ``--resume <id>``, a
    different mechanism than bare ``-c``, and is handled by the SDK runner /
    explicit resume path, not by the TUI ``-c`` toggle. Unknown / empty
    values are treated as fresh (fail-safe: never silently resume).
    """
    return str(session or "").strip().lower() == SESSION_CONTINUE


__all__ = [
    "SESSION_CONTINUE",
    "SESSION_FRESH",
    "SESSION_RESUME",
    "default_session_for_role",
    "role_wants_continuity",
    "wants_continue",
]
