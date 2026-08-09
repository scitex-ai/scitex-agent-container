"""Resolve the GitHub token for an agent container from the fleet secret pool.

WHY THIS EXISTS
---------------
The token lives in the operator's dotfiles secrets (``~/.bash.d/secrets``),
which ONLY a login shell sources. sac starts agent containers without a login
shell and, before this module, without forwarding the token — so ``gh`` inside
the container had neither an env token nor a ``~/.config/gh`` and reported
"not logged into any GitHub hosts".

Measured on scitex-compute-04, 2026-08-09 (lengths only, values never read)::

    login shell      GITHUB_TOKEN len=40   <- the secret IS present
    non-login shell  GITHUB_TOKEN len=0    <- it disappears here
    spec grep for GITHUB_TOKEN|GH_TOKEN    -> 0 matches
    ~/.config/gh/                          -> empty, no hosts.yml
    `gh` on the host                       -> command not found (it is in the SIF)

The cost was concrete: three agents each lost work time to it, and two finished,
tested fixes (PR #926, #927) had to be opened by a different session on their
authors' behalf. "Can push but cannot open a PR" is half a delivery loop.

DESIGN — deliberately the SAME shape as the Telegram bot token
--------------------------------------------------------------
``_cct_token_pool`` already solves exactly this problem for ``CCT_BOT_TOKEN``:
resolve from the secret-file pool at start, warn loudly and actionably when the
slot is missing, never write the value into a spec. This module reuses that
pool rather than inventing a second secrets path — one place to rotate, one
place to reason about.

SECURITY
--------
The caller emits the resolved value as ``--env GITHUB_TOKEN=...``. That is safe
ONLY because ``_apptainer_secret_env.redact_secret_env_to_file`` lifts
secret-shaped pairs out of the world-readable argv into a per-agent 0600
env-file before exec, and its predicate matches ``*_TOKEN`` — which both
``GITHUB_TOKEN`` and ``GH_TOKEN`` satisfy. If that predicate ever narrows, this
value would leak into ``/proc/<pid>/cmdline``; the test suite pins the
relationship so the coupling cannot rot silently.

This module NEVER logs the token value — only whether one was found, and its
length, which is what makes a "wrong token" diagnosable without exposing it.
"""

from __future__ import annotations

import logging

__all__ = [
    "GITHUB_TOKEN_VARS",
    "github_token_env_flags",
    "resolve_github_token",
]

logger = logging.getLogger(__name__)

#: Both spellings are forwarded. ``gh`` prefers ``GH_TOKEN`` and falls back to
#: ``GITHUB_TOKEN``; git credential helpers and most CI tooling read
#: ``GITHUB_TOKEN``. Setting one and not the other is a common half-fix that
#: works for one tool and mystifies the next.
GITHUB_TOKEN_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


def resolve_github_token(pool_env: dict[str, str] | None = None) -> str | None:
    """Return the GitHub token from the fleet secret pool, or ``None``.

    ``pool_env`` is injectable for tests; by default it is the same pool the
    Telegram bot token resolves from (``_cct_token_pool._pool_env``), which
    honours ``SAC_SECRETS_ENVRC`` and falls back to the canonical ``$HOME``
    pool so a cron / raw-ssh start still finds the secret files.

    Returns ``None`` — never an empty string — when nothing resolves, so the
    caller can distinguish "absent" from "present but empty". An empty value is
    treated as absent: a defined-but-empty ``GITHUB_TOKEN`` is exactly what the
    containers reported on 2026-08-09, and forwarding it would reproduce the
    bug while looking like a fix.
    """
    if pool_env is None:
        from ._cct_token_pool import _pool_env

        try:
            pool_env = _pool_env()
        except Exception as exc:  # pragma: no cover - defensive
            # A pool that cannot be read is UNKNOWN, not "no token". Say so.
            logger.warning(
                "github-token: could not read the secret pool (%s); "
                "`gh` inside the container will be unauthenticated",
                exc,
            )
            return None

    for var in GITHUB_TOKEN_VARS:
        value = (pool_env.get(var) or "").strip()
        if value:
            return value
    return None


def github_token_env_flags(
    *,
    agent_name: str,
    pool_env: dict[str, str] | None = None,
) -> list[str]:
    """``--env`` flags carrying the GitHub token, or ``[]`` with a loud warning.

    The warning is deliberately actionable and names the CONSEQUENCE rather
    than just the missing variable: an agent that learns at first ``gh pr
    create`` that it has no token has already finished the work it cannot
    deliver.
    """
    token = resolve_github_token(pool_env)
    if not token:
        logger.warning(
            "github-token: no GITHUB_TOKEN/GH_TOKEN in the secret pool for "
            "agent %r. THE AGENT STARTS NORMALLY, but `gh` inside the "
            "container will be unauthenticated, so `gh pr create` will fail "
            "with 'not logged into any GitHub hosts' — the agent can commit "
            "and push but cannot open a pull request. To fix: add "
            "GITHUB_TOKEN=<token> to a secrets file listed in "
            "SAC_SECRETS_ENVRC (the same pool CCT_BOT_TOKEN_<SLOT> uses), "
            "then restart this agent.",
            agent_name,
        )
        return []

    logger.info(
        "github-token: forwarding a %d-character token to agent %r as %s "
        "(value redacted into the per-agent 0600 env-file before exec)",
        len(token),
        agent_name,
        "/".join(GITHUB_TOKEN_VARS),
    )
    flags: list[str] = []
    for var in GITHUB_TOKEN_VARS:
        flags += ["--env", f"{var}={token}"]
    return flags
