"""Carry ``GITHUB_TOKEN`` from the fleet secrets pool into an agent container.

WHY — the gap this closes
-------------------------
Measured 2026-08-09 on scitex-compute-04, end to end::

    login shell      GITHUB_TOKEN len=40   <- the secret IS present and correct
    non-login shell  GITHUB_TOKEN len=0    <- it disappears here
    spec grep for GITHUB_TOKEN / GH_TOKEN  -> 0 matches
    ~/.config/gh/ on the host              -> EMPTY (no hosts.yml)

The token lives in the dotfiles secrets (``~/.bash.d/secrets``), which only a
LOGIN shell sources. sac starts containers without a login shell and without
passing the token through, so ``gh`` inside the container has neither an env
token nor a config and reports "not logged into any GitHub hosts".

The consequence is not cosmetic: on 2026-08-09 three agents finished tested
work and NONE could open a pull request. Two finished fixes had to be opened
by the operator's own session on their authors' behalf. "Can push, cannot open
a PR" is only half a delivery loop, and it needs a human every single time.

The secrets were NOT missing and NOT undeployed — they simply never crossed
into the container. That distinction matters: "deploy dotfiles harder" would
not have fixed this.

Deliberately reuses the ``CCT_BOT_TOKEN`` shape rather than inventing a second
secrets path: same pool (launching env overlaid with the ``SAC_SECRETS_ENVRC``
secret files), same ``dest/.env`` destination, same never-fatal / always-loud
contract, same rule that a hand-authored value wins.

CONTRACT
--------
* The token VALUE is never logged, never put in an argv, and never written to
  a spec. Specs are dotfiles-tracked and reach GitHub; the value comes from
  the runtime pool only.
* An unresolvable token WARNs naming exactly what will not work
  (``gh pr create``) and the fixes. It NEVER fails the boot — an agent without
  a token still does useful work, it just cannot open pull requests, and
  discovering that at first push is what cost three agents their afternoon.
* An existing value in ``dest/.env`` is authoritative and left untouched.
"""

from __future__ import annotations

from pathlib import Path

#: What ``gh`` actually reads. ``GH_TOKEN`` takes precedence in gh itself, but
#: ``GITHUB_TOKEN`` is the name the fleet secrets already use, so that is the
#: one we resolve FROM. Both are written so either lookup succeeds.
_TOKEN_VAR = "GITHUB_TOKEN"
_GH_ALIAS_VAR = "GH_TOKEN"


def _logger():
    """scitex-logging logger, imported lazily — same rationale as
    ``_cct_token_pool._logger``: the package auto-configures handlers on
    first import, which must not tax module import."""
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def ensure_github_token(config, dest: Path) -> None:
    """Inject ``GITHUB_TOKEN`` into ``dest/.env`` from the fleet secrets pool.

    Called from :func:`._to_home.deploy_to_home` AFTER the ``.envrc`` cascade
    fold, so an explicit hand-authored mapping always wins — the same ordering
    :func:`._cct_token_pool.ensure_cct_bot_token` relies on.

    Unlike the Telegram token this is NOT gated on a spec opt-in. Every agent
    that can push can, in principle, need to open a pull request, and requiring
    each spec to opt in would reproduce the failure this fixes: the agent finds
    out it cannot deliver only at the moment it tries.

    Never raises. Never logs the token value.
    """
    from ._secret_pool import (  # the shared pool toolkit, one source
        _pool_env,
        _pool_source_label,
        _read_env_file,
        _write_env_file,
    )

    agent_name = getattr(config, "name", "") or ""
    env_file = dest / ".env"
    existing = _read_env_file(env_file) if env_file.is_file() else {}

    if existing.get(_TOKEN_VAR) or existing.get(_GH_ALIAS_VAR):
        # Hand-authored .envrc (or a prior deploy) already provided it —
        # authoritative, exactly as for CCT_BOT_TOKEN.
        return

    pool = _pool_env()
    value = pool.get(_TOKEN_VAR, "") or pool.get(_GH_ALIAS_VAR, "")
    if value:
        # Write BOTH names. `gh` prefers GH_TOKEN; scripts and hooks around the
        # fleet read GITHUB_TOKEN. Writing one and not the other is how an
        # agent ends up with a token that the tool it needs cannot see.
        existing[_TOKEN_VAR] = value
        existing[_GH_ALIAS_VAR] = value
        _write_env_file(env_file, existing)
        _logger().info(
            "github: resolved %s for agent %r from the fleet secrets pool "
            "(value not logged) -> %s; also written as %s.",
            _TOKEN_VAR,
            agent_name,
            env_file,
            _GH_ALIAS_VAR,
        )
        return

    _logger().warning(
        "github: no %s resolved for agent %r from the pool (%s). THE AGENT "
        "STARTS NORMALLY — this is NOT a startup failure. What will NOT work: "
        "`gh pr create`, `gh pr merge`, and anything else authenticating to "
        "GitHub through gh; the agent can still commit and push over SSH. "
        "Measured 2026-08-09: three agents each finished tested work and "
        "discovered this only at PR time, so it is warned HERE, at start, "
        "rather than there. To fix, do ONE of: (1) add %s=<token> to a secrets "
        "file listed in the canonical pool (restart `sac listen` afterwards if "
        "it provides the env), or (2) export %s via the project's .envrc. Do "
        "NOT put the value in a spec — specs are dotfiles-tracked and reach "
        "GitHub.",
        _TOKEN_VAR,
        agent_name,
        _pool_source_label(),
        _TOKEN_VAR,
        _TOKEN_VAR,
    )


__all__ = ["ensure_github_token"]
