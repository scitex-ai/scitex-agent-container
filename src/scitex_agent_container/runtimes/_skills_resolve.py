"""Materialize the host ``~/.claude/skills/`` symlinks into the container
``$HOME`` as **symlink-resolved real copies**.

Why this exists
---------------
The host's ``~/.claude/skills/`` is usually a directory of symlinks
into per-project source trees on the host filesystem
(e.g. ``general`` → ``~/proj/scitex-dev/src/scitex_dev/_skills/general/``).
The previous delivery path was a read-only bind mount of that whole
directory into the container, which has a fatal flaw under apptainer
``--containall``: the host ``proj/`` targets are not visible inside
the container, so every symlink dangles and the agent cannot read the
skill content. The same dangling-symlink failure happens whenever the
container HOME is on an overlay or volume that doesn't mirror the host
filesystem layout 1:1.

The fix is to deliver skills the same way every other to_home payload
is delivered (see ADR-0006 and PR #149): write real on-disk content
into the container ``$HOME`` from the host runtime *before* launch.
Each host entry under ``~/.claude/skills/<name>`` is resolved to its
real path on the host (via ``Path.resolve``) and copied to
``<dest_home>/.claude/skills/<name>/`` with
``shutil.copytree(symlinks=False)`` so every embedded symlink is
dereferenced too — the resulting tree is fully self-contained inside
the container.

Wire-up
-------
:func:`deploy_host_skills_resolved` is called from
:func:`_to_home.deploy_to_home` and :func:`_to_home.materialize_to_home`
**before** the baseline + per-agent ``to_home/`` walks. Order matters:
to_home content layered on top can still override any host-resolved
skill (overlay semantics). Specs that ship their own ``.claude/skills/``
under ``to_home/`` win on conflict — host-resolved skills only fill the
gaps.

Source-dir resolution
---------------------
:func:`resolve_host_skills_dir` reads ``$SAC_HOST_SKILLS_DIR`` first
(absolute path; primarily for tests and CI runners that do not have
a real ``~/.claude/skills``), then falls back to the standard
``~/.claude/skills`` location. Returns ``None`` when neither exists.

Failure modes — fail loudly, never silently
-------------------------------------------
A dangling top-level symlink is **skipped with a warning** — copying
into a non-existent target would itself raise ``FileNotFoundError`` and
the operator already knows about the broken link (it was broken on the
host, not by us). Every skipped entry is logged at WARNING. Embedded
symlinks that dangle inside the resolved tree do raise during the copy
— that is a structural defect in the source skill the operator must
fix, not something to paper over with a partial materialization.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


# Env override for the host source dir. Used by tests and by CI runners
# that don't carry a real ``~/.claude/skills``. Absolute path.
_HOST_SKILLS_ENV_VAR = "SAC_HOST_SKILLS_DIR"


def resolve_host_skills_dir() -> Path | None:
    """Resolve the host directory that holds the user's Claude skills.

    Order:
      1. ``$SAC_HOST_SKILLS_DIR`` (absolute) — override.
      2. ``~/.claude/skills`` — the standard Claude Code location.

    Returns ``None`` when neither path exists or is not a directory.
    """
    override = (os.environ.get(_HOST_SKILLS_ENV_VAR, "") or "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    p = Path.home() / ".claude" / "skills"
    return p if p.is_dir() else None


def deploy_host_skills_resolved(
    dest_home: Path,
    host_skills_dir: Path | None = None,
) -> list[str]:
    """Materialize each entry under the host skills dir as a real,
    symlink-resolved tree at ``<dest_home>/.claude/skills/<name>/``.

    Parameters
    ----------
    dest_home:
        Host-side directory that maps to the container ``$HOME``
        (workspace home for hardened specs; overlay upper home for
        relaxed specs — both ultimately reach the container ``$HOME``).
    host_skills_dir:
        Optional explicit source dir. When ``None``, falls back to
        :func:`resolve_host_skills_dir`.

    Returns
    -------
    list[str]
        Skill names delivered, in sorted order. Empty when no source
        dir resolves or it contains no entries.

    Semantics
    ---------
    * Each top-level entry is resolved via ``Path.resolve(strict=False)``
      and only materialized if the resolved target exists.
    * A directory entry is copied with ``shutil.copytree(symlinks=False)``
      so nested symlinks are dereferenced inside the resulting tree —
      no host paths leak into the container view.
    * A regular file entry (e.g. ``SKILL.md`` at the top level) is
      copied with ``shutil.copy2(follow_symlinks=True)``.
    * Idempotent: an existing destination entry (dir, file, or symlink)
      is removed before the new copy is written. Repeated calls always
      land the current host content.
    * Dangling top-level entries are skipped with a WARNING; no
      partial / empty placeholder is left at the destination.
    """
    src = host_skills_dir if host_skills_dir is not None else resolve_host_skills_dir()
    if src is None or not src.is_dir():
        return []
    dest_skills = dest_home / ".claude" / "skills"
    delivered: list[str] = []
    for entry in sorted(src.iterdir()):
        name = entry.name
        try:
            resolved = entry.resolve(strict=False)
        except OSError as exc:  # stx-allow: fallback (reason: filesystem op failure)
            logger.warning(
                "skills: cannot resolve %s: %s — skipped", entry, exc
            )
            continue
        if not resolved.exists():
            logger.warning(
                "skills: %s -> %s (dangling target) — skipped", entry, resolved
            )
            continue
        dest_skills.mkdir(parents=True, exist_ok=True)
        dst = dest_skills / name
        _replace_dst(dst)
        if resolved.is_dir():
            shutil.copytree(resolved, dst, symlinks=False)
        else:
            shutil.copy2(resolved, dst, follow_symlinks=True)
        delivered.append(name)
        logger.info(
            "skills: materialized %s -> %s (resolved from %s)",
            name,
            dst,
            resolved,
        )
    return delivered


def _replace_dst(dst: Path) -> None:
    """Remove ``dst`` (dir, file, or symlink) if it exists.

    Leaves a clean slot for the new resolved copy. A symlink (including
    one whose target no longer exists) is unlinked. A real directory is
    recursively removed. Idempotent — no-op when ``dst`` is absent.
    """
    if dst.is_symlink():
        dst.unlink()
        return
    if not dst.exists():
        return
    if dst.is_dir():
        shutil.rmtree(dst)
    else:
        dst.unlink()


__all__ = [
    "deploy_host_skills_resolved",
    "resolve_host_skills_dir",
]
