"""Materialize ``<spec_dir>/to_home/`` into the agent's workspace ``$HOME``.

The single canonical layout for materializing files into an agent's
``$HOME`` (see ADR-0006). The layout makes the rule explicit:

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
    deploy with :class:`WorkspaceCLAUDEMarkerError`.
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
laid down (full overwrite, marker-protected re-wrap, symlink
dereference-copy).

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
import re
import shutil
import stat
from datetime import datetime
from pathlib import Path

from ..config import AgentConfig
from ._symlink_resolve import DanglingToHomeSymlinkError, deref_copy_symlink

logger = logging.getLogger(__name__)

END_MARKER = "<!-- End of scitex-agent-container generated section -->"
START_MARKER_PREFIX = "<!-- Start of scitex-agent-container generated section"


class WorkspaceCLAUDEMarkerError(RuntimeError):
    """Existing workspace marker-protected file has malformed markers.

    The deploy is hard-aborted on this error rather than silently
    overwriting or guessing — preserving user content past the End
    marker is a safety contract and any ambiguity in marker placement
    could destroy work.
    """


def _validate_marker_invariants(text: str, source_name: str) -> None:
    """Hard-fail if Start/End markers are missing or malformed."""
    start_count = text.count(START_MARKER_PREFIX)
    end_count = text.count(END_MARKER)
    if start_count != 1 or end_count != 1:
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: expected exactly 1 Start marker and 1 End "
            f"marker, found Start={start_count} End={end_count}. "
            "Refusing to deploy to avoid data loss. Restore the markers "
            "manually before retrying."
        )
    if text.find(START_MARKER_PREFIX) > text.find(END_MARKER):
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: Start marker appears AFTER End marker. "
            "This indicates a corrupted file. Refusing to deploy."
        )


def _extract_user_tail(workspace_path: Path) -> str:
    if not workspace_path.exists():
        return ""
    try:
        existing = workspace_path.read_text()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return ""
    idx = existing.rfind(END_MARKER)
    if idx == -1:
        return ""
    return existing[idx + len(END_MARKER) :]


def _interpolate_env(text: str) -> str:
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def _interpolate_metadata(text: str, config: AgentConfig) -> str:
    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return config.name
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            return config.labels.get(label) or m.group(0)
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, text)


# Files that get marker-protected merge semantics (vs. full overwrite).
# Marker protection guards against silent data loss on a hand-edited file.
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

    Resolution order:
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

    Two-pass overlay:

      1. The shared/common baseline ``to_home/``.
      2. ``<spec_dir>/to_home/`` on top — so per-agent files win on
         conflict.

    Walks each tree and applies the per-entry semantics described in
    the module docstring (symlinks are dereference-copied to real
    content). Idempotent — safe to call on every agent start. The
    runtime never auto-reads host state; the definition is the sole
    source of truth.

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
    """``AgentConfig``-driven entrypoint for to_home materialization.

    Two-pass overlay:

      1. The shared/common baseline ``to_home/``.
      2. The per-agent ``to_home/`` on top — so per-agent files win on
         conflict.

    Resolves the per-agent directory via :func:`resolve_to_home_dir`
    (honours ``spec.to_home`` overrides) and the baseline via
    :func:`resolve_baseline_to_home_dir`, then applies metadata-aware
    interpolation (${metadata.name}, ${metadata.labels.*}, ${ENV_VAR})
    to text files. Symlinks are dereference-copied to real content; the
    runtime never auto-reads host state. No-op when neither the baseline
    nor the per-agent to_home resolves.
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
        # a directory through the mirror branch. The link is resolved to
        # its real target content (dereference-copy); a dangling target
        # hard-aborts via DanglingToHomeSymlinkError.
        if child.is_symlink():
            deref_copy_symlink(child, dst)
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


def _clear_readonly_dst(dst: Path) -> None:
    """Make an existing ``dst`` overwritable before a copy/write.

    Hooks deployed under ``to_home/`` are commonly mode 0755/read-only
    (e.g. ``hook_switch_helper.sh``). ``shutil.copy2`` / ``Path.write_text``
    over a read-only existing destination raise
    ``PermissionError: [Errno 13]`` — the deploy from #142 hit exactly
    this. We add the owner-write bit so the in-place overwrite succeeds.

    No-op when ``dst`` doesn't exist or is already writable. Symlinks are
    left untouched (the symlink path unlinks them instead). Genuinely
    unexpected ``OSError`` (e.g. EROFS, EPERM on a foreign-owned file) is
    re-raised so the deploy still crashes loud rather than masking a real
    permissions problem.
    """
    if dst.is_symlink() or not dst.exists():
        return
    mode = dst.stat().st_mode
    if not mode & stat.S_IWUSR:
        os.chmod(dst, mode | stat.S_IWUSR)


# Symlinks are dereference-copied to real content — see
# :func:`_symlink_resolve.deref_copy_symlink`. The traversal calls it
# directly; this module re-exports the symbol for callers.


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
    _clear_readonly_dst(dst)
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
    _clear_readonly_dst(dst)
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

    Invariants:
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
    # with nested Start/End pairs after wrapping (defensive scrub for
    # CLAUDE.md).
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
        # Shouldn't happen — we just wrote END_MARKER — but keep the
        # safety net so the contract is explicit.
        updated = new_content
    elif user_tail:
        updated = new_content.rstrip("\n") + user_tail
        if not updated.endswith("\n"):
            updated += "\n"
    else:
        updated = new_content

    dst.parent.mkdir(parents=True, exist_ok=True)
    _clear_readonly_dst(dst)
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


__all__ = [
    "DanglingToHomeSymlinkError",
    "WorkspaceCLAUDEMarkerError",
    "deploy_to_home",
    "materialize_to_home",
    "resolve_baseline_to_home_dir",
    "resolve_to_home_dir",
]
