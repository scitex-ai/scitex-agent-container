"""Deliver the ``to_home`` tree into a relaxed apptainer overlay ``$HOME``.

Companion to :mod:`_to_home`. The default apptainer path binds the host
workspace home (``runtime/<name>/home/``) at the container ``$HOME`` via
``--bind <workspace_home>:/home/agent``, so :func:`_to_home.deploy_to_home`
writing into the workspace home is enough — the bind makes the tree visible.

Relaxed specs (``apptainer.relaxed: true``) opt out of sac's hardened flags
and instead declare their own ``raw_args`` — typically
``--containall --home /home/agent --overlay <dir>``. Under that combo the
operator-declared ``--home /home/agent`` sets a container HOME that is
satisfied by the overlay's upper layer, NOT by the earlier workspace-home
bind. The result: the materialized ``to_home`` tree never reaches the
container ``$HOME`` — hooks/settings/.bashrc/etc. silently absent.

Fix: materialize the SAME tree (1:1, all files — not just ``.claude``) into
the overlay's upper directory at ``<overlay>/upper/<container_home>/`` BEFORE
launch, so the whole tree is part of the container filesystem. Skills arrive
as real materialized content under ``.claude/skills/`` (the explicit
``to_home`` symlink is dereference-copied by :mod:`_to_home`; see
:mod:`_symlink_resolve`), so there is no separate read-only skills bind to
layer on top and nothing to shadow.

Only directory-form overlays support an upper layer. ``.img`` overlays are
loopback ext3 images we cannot write into from the host without mounting
them, so this delivery is a no-op for them (the caller falls back to the
workspace-home bind, which an ``.img`` overlay does not shadow because such
specs are not the relaxed ``--home``-override pattern).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import AgentConfig
from ._to_home import deploy_to_home

logger = logging.getLogger(__name__)

# Apptainer directory overlays keep the writable layer under ``upper/``
# (sibling ``work/`` is scratch). See ``apptainer overlay create`` docs.
_OVERLAY_UPPER_DIRNAME = "upper"

# Canonical container HOME — matches the D5 preflight invariant and the
# hardened-mode ``--home /home/agent`` default. Used when the spec's raw_args
# do not declare an explicit ``--home``.
DEFAULT_CONTAINER_HOME = "/home/agent"


def resolve_container_home(config: AgentConfig) -> str:
    """Resolve the in-container ``$HOME`` for ``config``.

    Reads ``--home <path>`` from ``spec.apptainer.raw_args`` (operator's
    explicit override under relaxed mode). Falls back to
    :data:`DEFAULT_CONTAINER_HOME` — the canonical operator-independent HOME
    that hardened mode auto-prepends.

    Always returns an absolute POSIX path string; never reads the host
    operator's environment (machine-independence).
    """
    ap = getattr(config, "apptainer", None)
    raw = list(getattr(ap, "raw_args", None) or []) if ap is not None else []
    for i, arg in enumerate(raw):
        if arg == "--home" and i + 1 < len(raw):
            val = str(raw[i + 1]).strip()
            if val:
                return val
    return DEFAULT_CONTAINER_HOME


def _resolve_overlay_dir(config: AgentConfig) -> Path | None:
    """Resolve the apptainer overlay path to an absolute directory, or None.

    Sources, in order:
      1. ``spec.apptainer.overlay`` (the modeled field), or
      2. ``--overlay <path>`` in ``spec.apptainer.raw_args`` (the escape
         hatch the relaxed pattern uses).

    Relative paths resolve against ``spec.workdir`` (matching
    :meth:`ApptainerContainerRuntime.build_run_argv`). Returns ``None`` when
    no overlay is declared, or when the declared overlay path exists but is
    not a directory (e.g. an ``.img`` loopback image we cannot write into
    from the host).
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        return None
    raw_overlay = (getattr(ap, "overlay", "") or "").strip()
    if not raw_overlay:
        raw = list(getattr(ap, "raw_args", None) or [])
        for i, arg in enumerate(raw):
            if arg == "--overlay" and i + 1 < len(raw):
                raw_overlay = str(raw[i + 1]).strip()
                break
    if not raw_overlay:
        return None
    p = Path(raw_overlay).expanduser()
    if not p.is_absolute():
        workdir = getattr(config, "workdir", "") or ""
        if not workdir:
            return None
        p = Path(workdir).expanduser() / p
    # Directory-form overlay only. An existing path must be a dir; a
    # not-yet-created path is treated as a dir (apptainer creates the
    # upper/ tree on first launch — we pre-create it here).
    if p.exists() and not p.is_dir():
        return None
    return p


def resolve_overlay_upper_home(config: AgentConfig) -> Path | None:
    """Resolve ``<overlay>/upper/<container_home>/`` for relaxed+overlay specs.

    Returns the host-side directory where the ``to_home`` tree must be
    materialized so it appears at the container ``$HOME``. Returns ``None``
    when the spec is not the relaxed-directory-overlay pattern (no overlay,
    or an ``.img`` overlay) — the caller then relies on the workspace-home
    bind instead.

    The container home (from :func:`resolve_container_home`) is an absolute
    path like ``/home/agent``; its leading slash is stripped so it joins
    under ``<overlay>/upper/``.
    """
    overlay_dir = _resolve_overlay_dir(config)
    if overlay_dir is None:
        return None
    container_home = resolve_container_home(config).lstrip("/")
    if not container_home:
        return None
    return overlay_dir / _OVERLAY_UPPER_DIRNAME / container_home


def deploy_to_home_overlay(config: AgentConfig) -> Path | None:
    """Materialize the ``to_home`` tree into the overlay upper home.

    Resolves the destination via :func:`resolve_overlay_upper_home` and, when
    applicable, runs the same two-pass overlay materialization
    :func:`_to_home.deploy_to_home` performs (baseline first, per-agent on
    top, 1:1 for every file — not just ``.claude``).

    Returns the destination directory it wrote into, or ``None`` when the
    spec is not a relaxed-directory-overlay spec (no-op).
    """
    dest = resolve_overlay_upper_home(config)
    if dest is None:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    deploy_to_home(config, str(dest))
    logger.info("to_home: mirrored into overlay upper home %s", dest)
    # Record the overlay's scitex-* package delta as a version manifest so a
    # later ``sac versions`` reads it cheaply (source="manifest") instead of
    # re-scanning the overlay venv. Best-effort provenance side-effect — a
    # failure here must never affect the launch.
    try:
        from .._drift.versions import record_overlay_manifest

        record_overlay_manifest(config)
    except Exception:  # stx-allow: fallback (reason: version-manifest recording is best-effort; never break a launch)
        logger.debug("to_home: overlay version-manifest recording skipped", exc_info=True)
    return dest


__all__ = [
    "DEFAULT_CONTAINER_HOME",
    "deploy_to_home_overlay",
    "resolve_container_home",
    "resolve_overlay_upper_home",
]
