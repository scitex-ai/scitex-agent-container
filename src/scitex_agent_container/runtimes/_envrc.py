"""Evaluate an agent's ``to_home/.envrc`` into ``KEY=VALUE`` env pairs.

direnv's ``.envrc`` is a shell script (``export``, ``$(...)``, conditionals,
``dotenv`` …), so apptainer ``--env-file`` cannot parse it directly. At deploy
time :func:`_to_home.deploy_to_home` sources it in bash — AFTER the sibling
``.env`` so the ``.envrc`` can read and override those values — and captures
the NET environment contribution (vars ADDED or CHANGED vs a baseline shell
with the same parent env). The result is folded back into the materialised
``$HOME/.env`` that :func:`_apptainer_build_argv.build_run_argv` injects via
``--env-file``.

Default behaviour (NOT opt-in): a present ``.envrc`` is always evaluated; when
the agent ships neither ``.env`` nor ``.envrc`` the whole flow is skipped.
Fail-loud: a ``.envrc`` whose bash evaluation exits non-zero raises
:class:`EnvrcEvalError` and aborts the deploy, rather than launching the agent
with a half-applied environment.

Secrets preamble (``SAC_SECRETS_ENVRC``): an agent's ``.envrc`` typically does
``export CCT_BOT_TOKEN="$CCT_BOT_TOKEN_TODO"`` — which only resolves when the
LAUNCHING process already carries the source secret. The operator's shell does;
the ``sac-listen`` daemon's does NOT, so a daemon-restarted agent folds an EMPTY
token. Set ``SAC_SECRETS_ENVRC`` to a colon-separated list of absolute secret
files (non-existent entries are skipped) and each is sourced (``set -a``) into
BOTH the baseline AND the loaded shells, BEFORE everything else. Because the
secret files' own vars then appear in BOTH shells they are NOT in the diff →
NOT folded into the agent ``.env`` (no leak); but the per-agent ``.envrc``'s
references now resolve to the real values and, being NEW/CHANGED keys, ARE
folded. A secret file that exists but fails to source still raises (fail-loud);
only a NON-EXISTENT path is skipped (matching the per-layer skip-if-missing
posture).

CANONICAL DEFAULT (class fix, 2026-07-18): ``start``/``restart`` used to TRUST
the caller's environment for the pool — so ``SAC_SECRETS_ENVRC`` had to be set
by whoever launched the process. It is baked into ``sac-listen.service`` but
NOT into a cron line, a raw ``ssh`` restart, or a federated ``scitex_dev.jobs``
timer (JobSpec cannot inject an environment). Any such caller folded EMPTY
CCT/Telegram tokens and thereby STRIPPED them on redeploy (confirmed live: an
``auth-heal.py`` cron restart and a raw-ssh restart both stripped cards+hub).
:func:`resolve_secret_files` is the caller-independent resolver: an explicit
``SAC_SECRETS_ENVRC`` wins verbatim, and when it is unset/empty it falls back to
the operator's standardized secret files (``$HOME/.bash.d/secrets/010_scitex/
*.src``) — the SAME default ``scripts/systemd/install-sac-listen.sh`` computes.
The CCT pool resolver (``_cct_token_pool._pool_env``) uses it, so any caller
re-resolves the bot token from the default pool AFTER the fold — no more
stripping. The general ``.envrc`` fold preamble (:func:`_secrets_preamble_lines`)
deliberately stays env-ONLY: it runs in a strict bash and must not couple every
deploy to host ``$HOME`` state; the CCT re-resolution (fail-open) is what heals
the reported incident. A host without that directory resolves to nothing (a safe
no-op, exactly as before).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

from ._to_home_text import _is_legacy_identity_var

logger = logging.getLogger(__name__)

# Shell-internal vars that legitimately differ between two otherwise-identical
# bash invocations (or are set by bash itself) — never part of the
# ``.env``/``.envrc`` contribution, so excluded from the captured diff.
_SHELL_NOISE = frozenset({"_", "SHLVL", "PWD", "OLDPWD"})

# Env var naming the secrets-preamble files (colon-separated absolute paths).
_SECRETS_ENVRC_VAR = "SAC_SECRETS_ENVRC"

# Canonical default pool location, resolved relative to ``$HOME`` — the SAME
# glob ``scripts/systemd/install-sac-listen.sh::secrets_envrc_value`` bakes into
# the listen unit. Used ONLY when ``SAC_SECRETS_ENVRC`` is unset/empty, so the
# CCT/Telegram pool is found no matter which caller (cron, raw ssh, a federated
# timer) launched the restart — not just the one process the operator's shell or
# the listen unit happened to export the var into. See the module docstring.
_DEFAULT_SECRETS_GLOB = ".bash.d/secrets/010_scitex/*.src"


class EnvrcEvalError(RuntimeError):
    """An agent's ``.envrc`` failed to evaluate (fail-loud; aborts deploy)."""


def resolve_secret_files(
    *, environ: "dict[str, str] | None" = None, home: "Path | None" = None
) -> list[Path]:
    """The secret files the preamble sources, honouring the canonical default.

    Precedence — and the whole point of the 2026-07-18 class fix:

    1. An explicit non-empty ``SAC_SECRETS_ENVRC`` wins VERBATIM: its
       colon-separated paths, keeping only the entries that currently exist (a
       non-existent path is skipped — the per-layer skip-if-missing posture).
       An operator/inherited value is never overridden.
    2. Otherwise (unset OR empty) fall back to the operator's standardized
       secret files ``$HOME/.bash.d/secrets/010_scitex/*.src`` (sorted, existing
       only) — the SAME default the listen-unit installer computes. This is what
       makes the pool CALLER-INDEPENDENT: a cron/raw-ssh/federated-timer restart
       that never had the var exported still loads the pool instead of folding
       (and thereby STRIPPING) every CCT/Telegram token.

    A host without that directory resolves to an empty list — a safe no-op,
    exactly the pre-fix behaviour for a caller with no pool. ``environ`` / ``home``
    are injectable so the resolution is unit-testable without touching the real
    process environment or ``$HOME``.
    """
    env = environ if environ is not None else os.environ
    raw = (env.get(_SECRETS_ENVRC_VAR) or "").strip()
    if raw:
        return [Path(e) for e in raw.split(":") if e and Path(e).is_file()]
    base = Path(home) if home is not None else Path(env.get("HOME") or Path.home())
    return sorted(p for p in base.glob(_DEFAULT_SECRETS_GLOB) if p.is_file())


def _secrets_preamble_lines() -> list[str]:
    """Return ``. <path>`` source lines from an EXPLICIT ``SAC_SECRETS_ENVRC``.

    Env-ONLY by design (unset var ⇒ empty preamble, unchanged pre-fix
    behaviour): the general ``.envrc`` fold sources these in a strict
    ``--noprofile --norc`` bash, so it must stay a pure function of the
    explicitly-configured var and never couple a deploy to host ``$HOME``
    state. The canonical ``$HOME`` default fallback lives in
    :func:`resolve_secret_files` and is applied only by the CCT pool resolver
    (``_cct_token_pool._pool_env``) — which is fail-open and re-resolves the
    bot token AFTER the fold, so an unset var no longer strips it.

    The same list is spliced into BOTH the baseline AND the loaded shell so the
    secret files' own vars cancel in the diff (no leak) while the per-agent
    ``.envrc``'s references still resolve.
    """
    raw = os.environ.get(_SECRETS_ENVRC_VAR, "")
    lines: list[str] = []
    for entry in raw.split(":"):
        if not entry:
            continue
        if Path(entry).is_file():
            lines.append(f". {shlex.quote(entry)}")
    return lines


def _capture_env(script: str, cwd: Path) -> dict[str, str]:
    """Run ``script`` in a clean (no rc/profile) bash and return its env.

    Output is parsed from ``env -0`` (NUL-delimited ``KEY=VALUE``) so values
    containing newlines or ``=`` survive intact. A non-zero exit raises
    :class:`EnvrcEvalError` carrying bash's stderr.
    """
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise EnvrcEvalError(proc.stderr.strip() or f"bash exited {proc.returncode}")
    env: dict[str, str] = {}
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        key, sep, val = entry.partition("=")
        if sep:
            env[key] = val
    return env


def eval_envrc(envrc: Path, *, base_env: Path | None = None) -> dict[str, str]:
    """Return the net env vars from sourcing ``base_env`` then ``envrc``.

    Sources (with ``set -a`` so plain ``KEY=VALUE`` assignments export) the
    optional ``base_env`` (the sibling ``.env``) first, then ``envrc``, and
    diffs the resulting environment against a baseline shell — returning only
    the keys the scripts ADDED or CHANGED (shell-internal noise filtered).
    ``cwd`` is ``envrc``'s directory so relative paths inside it resolve.

    Raises :class:`EnvrcEvalError` (wrapping bash stderr) on a non-zero exit.
    """
    cwd = envrc.parent
    preamble = _secrets_preamble_lines()
    baseline_lines = ["set -a", *preamble, "set +a", "env -0"]
    baseline = _capture_env("\n".join(baseline_lines), cwd)
    lines = ["set -a", *preamble]
    if base_env is not None and base_env.is_file():
        lines.append(f". {shlex.quote(str(base_env))}")
    lines.append(f". {shlex.quote(str(envrc))}")
    lines.append("set +a")
    lines.append("env -0")
    loaded = _capture_env("\n".join(lines), cwd)
    return _folded_env(loaded, baseline)


def _folded_env(loaded: dict[str, str], baseline: dict[str, str]) -> dict[str, str]:
    """Filter a captured ``loaded`` env down to the net ``.env`` contribution.

    Applied identically by :func:`eval_envrc` and :func:`eval_envrc_cascade`.
    A key is kept only when it is NOT shell noise, was ADDED or CHANGED vs the
    ``baseline`` shell, has a non-empty value, and is NOT a deprecated identity
    alias.

    * **Empty values** are dropped: a secret line
      (``export CCT_BOT_TOKEN="$CCT_BOT_TOKEN_X"``) folds to ``CCT_BOT_TOKEN=``
      when its source is unset at fold time, which then SHADOWS the real value
      a later layer supplies — an empty short-form made the telegram bridge
      read "" and 404 (dead poller).
    * **Legacy identity aliases** (:func:`_is_legacy_identity_var`) are dropped
      so a stale ``SCITEX_TODO_AGENT`` that once landed in ``dest/.env`` cannot
      SELF-PERPETUATE: the fold sources ``dest/.env`` as its base, so any such
      orphan var (set by no cascade ``.envrc``) re-enters the diff and is
      re-baked every deploy — and the card MCP now HARD-REJECTS any call
      when ``SCITEX_TODO_AGENT`` is present (INCIDENT 2026-07-05/06 write-
      outage). The current ``_ID`` identity vars are deliberately NOT dropped:
      the ``.env`` is the container ``--env-file`` and the materialized
      ``.mcp.json`` expands ``${SCITEX_CARDS_AGENT_ID}`` from it.
    """
    return {
        key: val
        for key, val in loaded.items()
        if key not in _SHELL_NOISE
        and baseline.get(key) != val
        and val != ""
        and not _is_legacy_identity_var(key)
    }


def fold_envrc_into_env(dest: Path) -> None:
    """Fold ``dest/.envrc`` (if any) into ``dest/.env`` for ``--env-file``.

    When ``dest/.envrc`` exists, evaluate it (after ``dest/.env`` so it can
    build on those values) and rewrite ``dest/.env`` with the combined net
    environment (``chmod 0600``). No-op when there is no ``.envrc`` — a plain
    ``.env`` is left exactly as materialised. Also a no-op when evaluation
    yields nothing, so an existing ``.env`` is never blanked.
    """
    envrc = dest / ".envrc"
    if not envrc.is_file():
        return
    env_file = dest / ".env"
    base = env_file if env_file.is_file() else None
    merged = eval_envrc(envrc, base_env=base)
    if not merged:
        return
    body = "".join(f"{k}={v}\n" for k, v in sorted(merged.items()))
    env_file.write_text(body)
    try:
        os.chmod(env_file, 0o600)
    except OSError as exc:  # stx-allow: fallback (reason: filesystem op failure)
        logger.warning("envrc: failed to chmod 0600 on %s: %s", env_file, exc)
    logger.info("envrc: folded %s into %s (%d vars)", envrc, env_file, len(merged))


def eval_envrc_cascade(
    envrcs: "list[Path | None]", *, base_env: Path | None = None
) -> dict[str, str]:
    """Net env from sourcing ``base_env`` then a CASCADE of ``.envrc`` files.

    ``envrcs`` is ordered LOWEST-precedence-first; ``None`` and non-existent
    entries are skipped, and duplicate files (same resolved path) collapse to
    their first occurrence. Each surviving file is sourced (``set -a``) after a
    ``cd`` into its own directory — so relative paths / ``$PWD`` inside it
    resolve, and a later layer overrides an earlier one. Returns only the keys
    ADDED or CHANGED vs a baseline shell (shell-internal noise filtered).

    Fail-loud: a ``.envrc`` whose bash evaluation exits non-zero raises
    :class:`EnvrcEvalError` carrying bash's stderr.
    """
    files: list[Path] = []
    seen: set[Path] = set()
    for p in envrcs:
        if p is None or not p.is_file():
            continue
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        files.append(p)
    if base_env is None and not files:
        return {}
    cwd = files[-1].parent if files else base_env.parent  # type: ignore[union-attr]
    preamble = _secrets_preamble_lines()
    baseline_lines = ["set -a", *preamble, "set +a", "env -0"]
    baseline = _capture_env("\n".join(baseline_lines), cwd)
    lines = ["set -a", *preamble]
    if base_env is not None and base_env.is_file():
        lines.append(f". {shlex.quote(str(base_env))}")
    for f in files:
        lines.append(f"cd {shlex.quote(str(f.parent))}")
        lines.append(f". {shlex.quote(f.name)}")
    lines.append("set +a")
    lines.append("env -0")
    loaded = _capture_env("\n".join(lines), cwd)
    return _folded_env(loaded, baseline)


def fold_envrc_cascade_into_env(dest: Path, envrcs: "list[Path | None]") -> None:
    """Fold an ordered ``.envrc`` CASCADE into ``dest/.env``.

    Generalises :func:`fold_envrc_into_env` from a single per-agent ``.envrc``
    to a precedence-ordered cascade (e.g. user → shared → workdir → per-agent),
    each layer overriding the previous. ``dest/.env`` (the already-materialised
    agent env) is the base, so nothing it carries is lost; the file is then
    rewritten (``chmod 0600``) with the combined net environment. No-op when
    the cascade contributes nothing, so an existing ``.env`` is never blanked.
    """
    env_file = dest / ".env"
    base = env_file if env_file.is_file() else None
    merged = eval_envrc_cascade(envrcs, base_env=base)
    if not merged:
        return
    body = "".join(f"{k}={v}\n" for k, v in sorted(merged.items()))
    env_file.write_text(body)
    try:
        os.chmod(env_file, 0o600)
    except OSError as exc:  # stx-allow: fallback (reason: filesystem op failure)
        logger.warning("envrc: failed to chmod 0600 on %s: %s", env_file, exc)
    n_layers = sum(1 for p in envrcs if p is not None and p.is_file())
    logger.info(
        "envrc: folded cascade (%d layers) into %s (%d vars)",
        n_layers,
        env_file,
        len(merged),
    )


__all__ = [
    "EnvrcEvalError",
    "eval_envrc",
    "eval_envrc_cascade",
    "fold_envrc_into_env",
    "fold_envrc_cascade_into_env",
    "resolve_secret_files",
]
