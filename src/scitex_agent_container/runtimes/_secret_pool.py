"""The fleet secrets POOL and the ``.env`` carrier it is folded into.

Extracted from :mod:`._cct_token_pool` (2026-08-10). Nothing here is specific to
Telegram: it is the generic machinery two token injectors share — read the pool,
say where the pool lives, read and rewrite the agent's materialised ``.env``.
:mod:`._github_token` was already importing all four names out of the CCT module
("shared pool, one source"), which is a GitHub-token module reaching into a
Telegram module for env-file I/O. Now both import from here and the CCT module
keeps only CCT policy.

The pool itself is the set of ``<PREFIX>_<SLOT>`` environment variables visible
to the launching ``sac agents start`` process — the union of its own environment
and the secret files listed in ``SAC_SECRETS_ENVRC`` (colon-separated absolute
paths), with a canonical ``$HOME`` fallback when that var is unset. Token VALUES
never appear in a log line; only slot names and paths do.
"""

from __future__ import annotations

import os
from pathlib import Path

# The .envrc secrets-preamble env var (shared with :mod:`._envrc`).
_SECRETS_ENVRC_VAR = "SAC_SECRETS_ENVRC"


def _logger():
    """scitex-logging logger, imported lazily (same rationale as
    ``config.__init__._config_logger``: the package auto-configures
    handlers on first import, which must not tax module import)."""
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a plain ``KEY=VALUE``-per-line env file (the fold's format).

    Tolerates blank lines and ``#`` comments; no shell semantics (the fold
    writes raw values, no quoting/export).
    """
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep:
            env[key.strip()] = val
    return env


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    """Rewrite ``path`` as sorted ``KEY=VALUE`` lines, owner-only perms."""
    body = "".join(f"{k}={v}\n" for k, v in sorted(env.items()))
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)


def _pool_env() -> dict[str, str]:
    """The pool: secret vars from the launching env, overlaid with the secret
    files (daemon-start path).

    Resolves the secret files via :func:`._envrc.resolve_secret_files`, which
    honours an explicit ``SAC_SECRETS_ENVRC`` AND — the 2026-07-18 class fix —
    falls back to the canonical ``$HOME`` default pool when the var is unset, so
    a cron / raw-ssh / federated-timer restart that never had the var exported
    still finds the bot token instead of folding (and STRIPPING) it. Sourced in
    a strict bash with the same ``set -a`` semantics as the ``.envrc`` fold.
    Falls back to the plain process env when no secret file resolves, and
    degrades to the process env (rather than failing the deploy) if a resolved
    secret file cannot be sourced — the caller's missing-token WARNING then
    names the pool source anyway.
    """
    import shlex

    from ._envrc import EnvrcEvalError, _capture_env, resolve_secret_files

    files = resolve_secret_files()
    if not files:
        return dict(os.environ)
    preamble = [f". {shlex.quote(str(p))}" for p in files]
    try:
        return _capture_env(
            "\n".join(["set -a", *preamble, "set +a", "env -0"]), Path.cwd()
        )
    except EnvrcEvalError as exc:  # stx-allow: fallback (reason: pool read must not abort deploy; missing token is reported loudly by the caller)
        _logger().warning(
            "secrets pool: failed to source %s (%s); falling back to the "
            "launching process env only.",
            _pool_source_label(),
            exc,
        )
        return dict(os.environ)


def _pool_source_label() -> str:
    """Human-readable pool location for log lines (paths only, no values)."""
    raw = os.environ.get(_SECRETS_ENVRC_VAR, "")
    if raw:
        return f"{_SECRETS_ENVRC_VAR}={raw}"
    # Class fix (2026-07-18): an unset var no longer means an empty pool — the
    # resolver falls back to the canonical ``$HOME`` default. Report THAT so the
    # missing-token WARN names where sac actually looked, not a pool it stopped
    # limiting itself to.
    from ._envrc import resolve_secret_files

    defaults = resolve_secret_files()
    if defaults:
        joined = ":".join(str(p) for p in defaults)
        return f"{_SECRETS_ENVRC_VAR} unset — using the canonical default pool {joined}"
    return (
        f"{_SECRETS_ENVRC_VAR} is UNSET and no canonical default pool files were "
        "found — pool limited to the launching process environment"
    )
