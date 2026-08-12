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
from dataclasses import dataclass
from pathlib import Path

# The .envrc secrets-preamble env var (shared with :mod:`._envrc`).
_SECRETS_ENVRC_VAR = "SAC_SECRETS_ENVRC"


@dataclass(frozen=True)
class PoolRead:
    """One read of the secrets pool, and whether a MISS in it means anything.

    A HIT is always conclusive — a slot we found is a slot that is there. The
    interesting question is the other direction, and it has THREE answers, not
    two: the slot is absent, or *sac never looked at the pool it meant to
    look at*. :attr:`trusted` is exactly that distinction, and collapsing it is
    the bug this class exists to prevent.

    THE MEASUREMENT BEHIND IT (2026-08-12, card
    ``sac-cct-token-slot-mismatch-and-env-fold-20260812``). After
    ``scitex-agent-container`` was relocated to a new host its Telegram rail
    vanished, and the first three diagnoses — mine — all said "there is no
    token on compute-04". Every one was wrong. The pool file was present on
    that host, complete, with all fifty secret files intact. What was missing
    was ``SAC_SECRETS_ENVRC`` in the *launching* ``sac-listen.service``
    environment, so the resolver found no secret file to source and sac read
    the bare process env instead. The operator's own correction is the whole
    specification for this field:

        「04 にトークンが無い」と私は言ったが誤り。**起動プロセスに無かった**
        が正しい。この区別がバグそのもの。

    ("I said 'there is no token on 04'. That was wrong — 'it was not in the
    LAUNCHING PROCESS' is right. That distinction IS the bug.")

    So ``trusted`` is False whenever sac sourced no pool FILE at all, even
    though :attr:`env` is still populated from the process environment. The
    process env can prove a slot present; it cannot prove one absent.

    Attributes
    ----------
    env
        The variables read. Values are secrets — never log, print, or embed
        them; check presence only.
    trusted
        Whether a MISS in :attr:`env` is CONCLUSIVE. True only when the
        canonical secret file(s) resolved AND sourced cleanly.
    detail
        Why the read is untrusted (empty when it is trusted), phrased for an
        operator-facing message. Paths only, never values.
    """

    env: dict[str, str]
    trusted: bool
    detail: str = ""


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


def read_pool() -> PoolRead:
    """Read the pool and say whether a MISS in it is CONCLUSIVE.

    Resolves the secret files via :func:`._envrc.resolve_secret_files`, which
    honours an explicit ``SAC_SECRETS_ENVRC`` AND — the 2026-07-18 class fix —
    falls back to the canonical ``$HOME`` default pool when the var is unset, so
    a cron / raw-ssh / federated-timer restart that never had the var exported
    still finds the bot token instead of folding (and STRIPPING) it. Sourced in
    a strict bash with the same ``set -a`` semantics as the ``.envrc`` fold.

    Three outcomes, and the middle one is the point (see :class:`PoolRead`):

    * secret file(s) resolved AND sourced cleanly → ``trusted=True``. A miss
      here means the slot really is not in the pool.
    * secret file(s) resolved but the source FAILED → ``trusted=False``. We
      hold the process env, which is not the pool we meant to read.
    * NO secret file resolved at all → ``trusted=False``. This is the
      relocation shape: the pool exists on disk and sac never opened it,
      because the launching process was not told where it lives. Reporting
      that as "the slot is absent" is the false negative that cost the
      operator his channel to an agent.

    Never raises — a pool that cannot be read is a verdict, not a crash, and
    the caller's missing-token message names the pool source either way.
    """
    import shlex

    from ._envrc import EnvrcEvalError, _capture_env, resolve_secret_files

    files = resolve_secret_files()
    if not files:
        return PoolRead(
            env=dict(os.environ),
            trusted=False,
            detail=(
                f"no canonical secret file resolved ({_pool_source_label()}), so "
                "sac read only the LAUNCHING PROCESS environment. That can prove "
                "a slot PRESENT but never proves one ABSENT — the pool file may "
                "be on this host, intact, and simply not visible to whatever "
                "started the agent (a systemd unit with no "
                f"{_SECRETS_ENVRC_VAR}, a non-interactive ssh, a cron tick)"
            ),
        )
    preamble = [f". {shlex.quote(str(p))}" for p in files]
    try:
        env = _capture_env(
            "\n".join(["set -a", *preamble, "set +a", "env -0"]), Path.cwd()
        )
    except EnvrcEvalError as exc:  # stx-allow: fallback (reason: pool read must not abort deploy; the failure is returned as an UNTRUSTED read so callers report "could not tell" rather than "absent")
        _logger().warning(
            "secrets pool: failed to source %s (%s); falling back to the "
            "launching process env only. A slot MISS against this read is "
            "INCONCLUSIVE, not evidence of absence.",
            _pool_source_label(),
            exc,
        )
        return PoolRead(
            env=dict(os.environ),
            trusted=False,
            detail=(
                f"the resolved secret file(s) ({_pool_source_label()}) could not "
                f"be sourced ({exc}), so sac fell back to the launching process "
                "environment. A miss against that read is inconclusive"
            ),
        )
    return PoolRead(env=env, trusted=True, detail="")


def _pool_env() -> dict[str, str]:
    """The pool as a plain mapping — :func:`read_pool` without the verdict.

    Kept for the callers that only ever ask "is this slot here?", where a hit
    is self-validating and the trust flag adds nothing. Anything that reports a
    MISS to a human must use :func:`read_pool` instead, so it can tell "absent"
    from "never looked".
    """
    return read_pool().env


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
