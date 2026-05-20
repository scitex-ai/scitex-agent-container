"""Materialize ``<spec_dir>/to_home/`` into the agent's workspace ``$HOME``.

Successor to :mod:`_dot_claude` (see ADR-0006). The new layout makes
the rule explicit:

    agents/<name>/
    ├── spec.yaml          (spec.to_home: ./to_home — default)
    └── to_home/           contents mirror $HOME 1:1
        ├── .claude/
        │   ├── CLAUDE.md          → $HOME/.claude/CLAUDE.md     (marker-protected)
        │   ├── settings.local.json
        │   ├── hooks/
        │   └── skills/
        ├── .mcp.json              → $HOME/.mcp.json             (full overwrite)
        ├── .env                   → $HOME/.env                  (overwrite, chmod 0600)
        ├── state.md               → $HOME/state.md              (marker-protected)
        └── secrets/               → $HOME/secrets/              (mirror, perms preserved)

Inside the container ``$HOME`` is bind-mounted from
``runtime/<name>/home/`` on the host — i.e. ``workspace_home`` in this
module's API.

Semantics per entry (see :func:`materialize_to_home`):

  - **CLAUDE.md** / **state.md** — marker-protected merge. Source is
    wrapped between Start/End markers; any user tail after the End
    marker is preserved. Malformed existing markers hard-abort the
    deploy (re-raised as :class:`WorkspaceCLAUDEMarkerError` from
    :mod:`_dot_claude`).
  - **.env** — full overwrite; chmod 0600 after write.
  - **Other regular files** — full overwrite (``shutil.copy2``).
  - **Directories** — recursed; structure preserved.
  - **Symlinks** — preserved as symlinks (target not resolved). Both
    absolute and relative targets pass through verbatim.

No fragmented "leaf-vs-mirror" distinction — every path under
``to_home/`` lands at the same relative path under ``workspace_home``.

Missing ``to_home/`` dir → silent no-op (specs without one just don't
get materialization).

Baseline layer (shared/common to_home)
--------------------------------------
Common hooks/settings shared by every agent live in ONE place instead
of being copied into every agent's ``to_home/``. Materialization is a
two-pass overlay:

  1. Apply the COMMON baseline ``to_home/`` first.
  2. Apply the per-agent ``<spec_dir>/to_home/`` ON TOP.

Per-agent files therefore win on conflict (overlay semantics) — they
re-run the same per-entry deploy helpers over whatever the baseline
laid down (full overwrite, marker-protected re-wrap, symlink replace).

Baseline location (see :func:`resolve_baseline_to_home_dir`):

  - ``$SAC_TO_HOME_BASELINE`` — explicit override (absolute dir), or
  - ``<agents_dir>/_base/to_home/`` — a sibling ``_base`` dir under the
    agents root (agents live at ``<agents_dir>/<name>/``, so the agents
    root is the spec dir's parent).

Absent baseline dir → behaves exactly as before (no baseline = current
behavior; fully backward compatible).
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from ..config import AgentConfig
from ._dot_claude import (
    END_MARKER,
    WorkspaceCLAUDEMarkerError,
    _extract_user_tail,
    _interpolate_env,
    _interpolate_metadata,
    _validate_marker_invariants,
)

logger = logging.getLogger(__name__)


# Files that get marker-protected merge semantics (vs. full overwrite).
# Same protection as the legacy dot_claude/CLAUDE.md path — never silent
# data loss on a hand-edited file.
_MARKER_PROTECTED_BASENAMES = frozenset({"CLAUDE.md", "state.md"})

# Files that get chmod 0600 after copy. ``.env`` only by default; the
# rest preserve source perms via ``shutil.copy2``.
_TIGHT_PERM_BASENAMES = frozenset({".env"})

# Env var: explicit override for the shared/common baseline to_home dir.
# Absolute path. When unset we fall back to ``<agents_dir>/_base/to_home``.
_BASELINE_ENV_VAR = "SAC_TO_HOME_BASELINE"

# Name of the sibling dir (under the agents root) that holds the common
# baseline. Agents live at ``<agents_dir>/<name>/``, so the agents root
# is the spec dir's parent and the baseline is ``<parent>/_base/to_home``.
_BASELINE_DIR_NAME = "_base"


# --- public API ------------------------------------------------------------


def resolve_to_home_dir(config: AgentConfig) -> Path | None:
    """Resolve ``spec.to_home`` to an absolute directory.

    Resolution mirrors :func:`_dot_claude.resolve_dot_claude_dir`:
      1. Absolute path: use as-is.
      2. Relative path: resolve against the directory containing
         ``spec.yaml``.
      3. Empty: auto-discover ``./to_home`` next to ``spec.yaml``.

    Returns ``None`` if no directory can be resolved (legacy specs
    without a to_home/ dir simply skip materialization).
    """
    spec_dir = _spec_dir(config)
    raw = (getattr(config, "to_home", "") or "").strip()
    if not raw:
        if spec_dir is not None and (spec_dir / "to_home").is_dir():
            return spec_dir / "to_home"
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        if spec_dir is None:
            return None
        p = spec_dir / p
    return p if p.is_dir() else None


def resolve_baseline_to_home_dir(spec_dir: Path | None) -> Path | None:
    """Resolve the shared/common baseline ``to_home/`` directory.

    Resolution order:
      1. ``$SAC_TO_HOME_BASELINE`` (absolute dir) — explicit override.
      2. ``<agents_dir>/_base/to_home`` — a sibling ``_base`` dir under
         the agents root. Agents live at ``<agents_dir>/<name>/``, so the
         agents root is ``spec_dir.parent``.

    Returns ``None`` when no baseline dir can be resolved (no baseline =
    current behavior; fully backward compatible).
    """
    override = (os.environ.get(_BASELINE_ENV_VAR, "") or "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    if spec_dir is None:
        return None
    p = spec_dir.parent / _BASELINE_DIR_NAME / "to_home"
    return p if p.is_dir() else None


def materialize_to_home(spec_dir: Path, workspace_home: Path) -> None:
    """Mirror ``<spec_dir>/to_home/`` into ``<workspace_home>/``.

    Two-pass overlay: the shared/common baseline ``to_home/`` is applied
    first, then ``<spec_dir>/to_home/`` is applied on top — so per-agent
    files win on conflict. Walks each tree and applies the per-entry
    semantics described in the module docstring. Idempotent — safe to
    call on every agent start.

    No-op when neither the baseline nor ``<spec_dir>/to_home/`` exists.
    """
    baseline = resolve_baseline_to_home_dir(spec_dir)
    root = spec_dir / "to_home"
    if baseline is None and not root.is_dir():
        return
    workspace_home.mkdir(parents=True, exist_ok=True)
    if baseline is not None:
        _walk_and_apply(baseline, baseline, workspace_home, config=None)
    if root.is_dir():
        _walk_and_apply(root, root, workspace_home, config=None)


def deploy_to_home(config: AgentConfig, workspace_home: str) -> None:
    """``AgentConfig``-driven entrypoint, parallel to
    :func:`_dot_claude.deploy_dot_claude`.

    Two-pass overlay: the shared/common baseline ``to_home/`` is applied
    first, then the per-agent ``to_home/`` is applied on top — so
    per-agent files win on conflict. Resolves the per-agent directory via
    :func:`resolve_to_home_dir` (honours ``spec.to_home`` overrides) and
    the baseline via :func:`resolve_baseline_to_home_dir`, then applies
    metadata-aware interpolation (${metadata.name}, ${metadata.labels.*},
    ${ENV_VAR}) to text files. No-op when neither directory resolves.
    """
    root = resolve_to_home_dir(config)
    baseline = resolve_baseline_to_home_dir(_spec_dir(config))
    if root is None and baseline is None:
        return
    dest = Path(workspace_home)
    dest.mkdir(parents=True, exist_ok=True)
    if baseline is not None:
        _walk_and_apply(baseline, baseline, dest, config=config)
    if root is not None:
        _walk_and_apply(root, root, dest, config=config)


# --- traversal -------------------------------------------------------------


def _walk_and_apply(
    src_root: Path,
    src_dir: Path,
    dst_dir: Path,
    *,
    config: AgentConfig | None,
) -> None:
    """Recursively replicate ``src_dir`` under ``dst_dir``.

    ``src_root`` is the original ``to_home/`` root, retained so log
    messages can report a path relative to the spec. ``config`` is
    optional: when present, text-file interpolation (``${metadata.*}``
    and ``${ENV_VAR}``) runs over CLAUDE.md / state.md / .env / .mcp.json.
    """
    for child in sorted(src_dir.iterdir()):
        rel = child.relative_to(src_root)
        dst = dst_dir / child.name

        # Symlinks first — must not follow into ``is_dir()`` / ``is_file()``
        # decisions, because is_dir(follow=True) would route a symlink to
        # a directory through the mirror branch.
        if child.is_symlink():
            _copy_symlink(child, dst)
            continue

        if child.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            _walk_and_apply(src_root, child, dst, config=config)
            continue

        # Regular file.
        if child.name in _MARKER_PROTECTED_BASENAMES:
            _deploy_marker_protected(child, dst, config=config, rel=rel)
        elif child.name in _TIGHT_PERM_BASENAMES:
            _deploy_tight_perm_file(child, dst, config=config, rel=rel)
        else:
            _deploy_plain_file(child, dst, config=config, rel=rel)


# --- per-entry deploy helpers ----------------------------------------------


def _copy_symlink(src: Path, dst: Path) -> None:
    """Preserve a symlink verbatim — never resolve the target.

    If ``dst`` exists (as link, file, or dir) it's removed first so the
    new symlink can be written in place. Relative and absolute targets
    both pass through unchanged.
    """
    target = os.readlink(src)
    if dst.is_symlink() or dst.exists():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            try:
                dst.unlink()
            except OSError as exc:  # stx-allow: fallback (reason: filesystem race)
                logger.warning("Failed to unlink %s: %s", dst, exc)
                return
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, dst)
    logger.info("to_home: symlink %s -> %s", dst, target)


def _read_and_interpolate(src: Path, config: AgentConfig | None) -> str:
    """Read ``src`` as text and apply metadata/env interpolation.

    Interpolation runs only when an ``AgentConfig`` is supplied (i.e. the
    public ``deploy_to_home`` entrypoint). The lower-level
    ``materialize_to_home(spec_dir, workspace_home)`` signature copies
    text verbatim — useful for unit tests and any caller that doesn't
    have a full AgentConfig in hand.
    """
    text = src.read_text()
    if config is not None and text.strip():
        text = _interpolate_metadata(text, config)
        text = _interpolate_env(text)
    return text


def _deploy_plain_file(
    src: Path,
    dst: Path,
    *,
    config: AgentConfig | None,
    rel: Path,
) -> None:
    """Full overwrite. Uses ``shutil.copy2`` for binary-safe perm preserve
    when no interpolation is needed; otherwise writes interpolated text."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if config is None:
        shutil.copy2(src, dst)
    else:
        # Best-effort interpolation: binary files raise UnicodeDecodeError
        # on read_text; fall back to byte copy in that case.
        try:
            text = _read_and_interpolate(src, config)
            dst.write_text(text)
            shutil.copystat(src, dst)
        except UnicodeDecodeError:
            shutil.copy2(src, dst)
    logger.info("to_home: deployed %s -> %s", rel, dst)


def _deploy_tight_perm_file(
    src: Path,
    dst: Path,
    *,
    config: AgentConfig | None,
    rel: Path,
) -> None:
    """Full overwrite, then chmod 0600 (e.g. ``.env``)."""
    text = _read_and_interpolate(src, config)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    dst.write_text(text)
    try:
        os.chmod(dst, 0o600)
    except OSError as exc:  # stx-allow: fallback (reason: filesystem op failure)
        logger.warning("Failed to chmod 0600 on %s: %s", dst, exc)
    logger.info("to_home: deployed (0600) %s -> %s", rel, dst)


def _deploy_marker_protected(
    src: Path,
    dst: Path,
    *,
    config: AgentConfig | None,
    rel: Path,
) -> None:
    """Marker-protected merge for CLAUDE.md / state.md.

    Mirrors :func:`_dot_claude._deploy_claude_md` invariants:
      - Source wrapped in Start/End markers.
      - Existing user content past the End marker is preserved.
      - Malformed existing markers (count != 1 or order swapped)
        hard-abort with :class:`WorkspaceCLAUDEMarkerError`.
    """
    section_content = src.read_text().strip()
    if not section_content:
        return

    if config is not None:
        section_content = _interpolate_metadata(section_content, config)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_tag = (
        f"<!-- Start of scitex-agent-container generated section ({timestamp}) -->"
    )

    # Strip any embedded sac markers from the source so we don't end up
    # with nested Start/End pairs after wrapping (same defensive scrub
    # _dot_claude applies to CLAUDE.md).
    import re

    section_body = re.sub(
        r"<!--.*?scitex-agent-container.*?-->\n?",
        "",
        section_content,
    ).strip()

    new_content = f"{start_tag}\n{section_body}\n{END_MARKER}\n"

    existing_text = dst.read_text() if dst.exists() else ""
    if existing_text.strip():
        _validate_marker_invariants(existing_text, str(dst))

    user_tail = _extract_user_tail(dst)

    if END_MARKER not in new_content:
        # Shouldn't happen — we just wrote END_MARKER — but mirror the
        # _dot_claude safety net so the contract is identical.
        updated = new_content
    elif user_tail:
        updated = new_content.rstrip("\n") + user_tail
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        updated = new_content

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(updated)
    logger.info(
        "to_home: marker-protected %s -> %s (user tail preserved: %s)",
        rel,
        dst,
        "yes" if user_tail else "no",
    )


# --- internal helpers ------------------------------------------------------


def _spec_dir(config: AgentConfig) -> Path | None:
    if not getattr(config, "config_path", ""):
        return None
    return Path(config.config_path).parent


# Re-export so callers can ``from _to_home import WorkspaceCLAUDEMarkerError``
# without also importing _dot_claude.
__all__ = [
    "WorkspaceCLAUDEMarkerError",
    "deploy_to_home",
    "materialize_to_home",
    "resolve_baseline_to_home_dir",
    "resolve_to_home_dir",
]
