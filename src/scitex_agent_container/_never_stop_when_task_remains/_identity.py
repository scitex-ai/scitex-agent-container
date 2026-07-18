"""Resolve WHICH agent this hook is running as, from the agent's own environment.

NEVER from the working directory. Directory-derived identity is the exact
bug PR #742 removed: a hook that infers "who am I" from ``Path.cwd().name``
reports a different agent every time the session cd's into a worktree, a
repo checkout, or ``/tmp``. It is worse than having no identity, because it
silently produces a CONFIDENT WRONG answer — the guard then queries some
other agent's board and either blocks on work that is not ours or allows a
stop while our own work is pending.

So the cascade is env-only, and an unresolved identity is an honest failure
(empty string) that the caller turns into a fail-OPEN allow — not a guess.

The order follows the identity vars sac injects (see
``_lifecycle/_rename_spec.ENV_RULES``, the SSOT for which env vars carry
the agent name), with the board identity first because that is the name the
detector keys its cards on:

1. ``SCITEX_CARDS_AGENT_ID`` — the current board-identity name. The
   deployed store already warns that ``SCITEX_TODO_*`` is honoured "for one
   transition window only", so prefer the new name where it is set.
2. ``SCITEX_TODO_AGENT_ID`` — today's injected board identity.
3. ``SCITEX_TODO_AGENT`` — its deprecated alias, still honoured upstream.
4. ``SAC_NAME`` — the container's own name, injected by
   ``listen_env_flags``.
5. ``SCITEX_AGENT_CONTAINER_NAME`` / ``SCITEX_AGENT_CONTAINER_AGENT`` /
   ``SAC_AGENT`` — older sac spellings, kept so a not-yet-renamed spec
   still resolves.
"""

from __future__ import annotations

import os

#: Env vars carrying this agent's identity, HIGHEST precedence first.
#: Deliberately env-only — there is no cwd entry and must never be one.
IDENTITY_ENV_VARS: tuple[str, ...] = (
    "SCITEX_CARDS_AGENT_ID",
    "SCITEX_TODO_AGENT_ID",
    "SCITEX_TODO_AGENT",
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_NAME",
    "SCITEX_AGENT_CONTAINER_AGENT",
    "SAC_AGENT",
)


def resolve_agent(flag: str = "", env: "dict[str, str] | None" = None) -> str:
    """Return this agent's id, or ``""`` when the environment does not say.

    ``flag`` (an explicit ``--agent``) wins so an operator or a test can
    name the agent directly. Otherwise the first non-empty variable in
    :data:`IDENTITY_ENV_VARS` wins.

    Returns ``""`` rather than guessing. The caller MUST treat that as
    "could not tell" and fail open — never as "no work pending", and never
    by falling back to the working directory.
    """
    if flag and flag.strip():
        return flag.strip()
    source = os.environ if env is None else env
    for key in IDENTITY_ENV_VARS:
        val = (source.get(key) or "").strip()
        if val:
            return val
    return ""


__all__ = ["IDENTITY_ENV_VARS", "resolve_agent"]
