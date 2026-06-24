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
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Shell-internal vars that legitimately differ between two otherwise-identical
# bash invocations (or are set by bash itself) — never part of the
# ``.env``/``.envrc`` contribution, so excluded from the captured diff.
_SHELL_NOISE = frozenset({"_", "SHLVL", "PWD", "OLDPWD"})


class EnvrcEvalError(RuntimeError):
    """An agent's ``.envrc`` failed to evaluate (fail-loud; aborts deploy)."""


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
    baseline = _capture_env("env -0", cwd)
    lines = ["set -a"]
    if base_env is not None and base_env.is_file():
        lines.append(f". {shlex.quote(str(base_env))}")
    lines.append(f". {shlex.quote(str(envrc))}")
    lines.append("set +a")
    lines.append("env -0")
    loaded = _capture_env("\n".join(lines), cwd)
    return {
        key: val
        for key, val in loaded.items()
        if key not in _SHELL_NOISE and baseline.get(key) != val
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
    baseline = _capture_env("env -0", cwd)
    lines = ["set -a"]
    if base_env is not None and base_env.is_file():
        lines.append(f". {shlex.quote(str(base_env))}")
    for f in files:
        lines.append(f"cd {shlex.quote(str(f.parent))}")
        lines.append(f". {shlex.quote(f.name)}")
    lines.append("set +a")
    lines.append("env -0")
    loaded = _capture_env("\n".join(lines), cwd)
    return {
        key: val
        for key, val in loaded.items()
        if key not in _SHELL_NOISE and baseline.get(key) != val
    }


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
]
