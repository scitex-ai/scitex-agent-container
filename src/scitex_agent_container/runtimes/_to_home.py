"""Materialize ``<spec_dir>/to_home/`` into the agent's workspace ``$HOME``.

The single canonical layout for materializing files into an agent's
``$HOME`` (see ADR-0006). The layout makes the rule explicit:

    agents/<name>/
    ├── spec.yaml          (spec.to_home: ./to_home — default)
    └── to_home/           contents mirror $HOME 1:1
        ├── .claude/
        │   ├── CLAUDE.md          → $HOME/.claude/CLAUDE.md     (marker-protected)
        │   ├── settings.json      → $HOME/.claude/settings.json (USER scope —
        │   │                        the interactive TUI reads hooks here; a
        │   │                        legacy settings.local.json is folded into
        │   │                        it by settings_json.setup_settings_json)
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

Baseline layer (shared/common to_home): two-pass overlay applies the
shared baseline first (resolved via :func:`resolve_baseline_to_home_dir`
— ``$SAC_TO_HOME_BASELINE`` override or ``<agents_dir>/_shared/to_home/``
sibling), then the per-agent ``to_home/`` on top. Per-agent files win
on conflict. Absent baseline → behaves as before, fully backward
compatible.

Credential-leak guard (2026-06-15, lead-reported): a ``.credentials.json``
under ``to_home/`` aborts the deploy with
:class:`WorkspaceCredentialLeakError`. Credentials are operator-rotated
runtime state from the auth-stage rw bind, never static workspace
content — see ``_to_home_errors.py`` for context.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
from datetime import datetime
from pathlib import Path

from ..config import AgentConfig
from ._envrc import fold_envrc_into_env
from ._mcp_merge import merge_mcp_json
from ._symlink_resolve import DanglingToHomeSymlinkError, deref_copy_symlink
from ._to_home_errors import (
    WorkspaceCLAUDEMarkerError,
    WorkspaceCredentialLeakError,
    WorkspaceMcpMergeError,
)
from ._to_home_text import (
    END_MARKER,
    extract_user_tail,
    interpolate_env,
    interpolate_metadata,
    split_around_generated_section,
    validate_marker_invariants,
)

logger = logging.getLogger(__name__)

# Marker constants + text helpers re-exported for legacy import paths
# (e.g. tests doing ``from ...runtimes._to_home import END_MARKER``).
# Implementations live in :mod:`_to_home_text` per the 2026-06-15
# extraction (see GITIGNORED/REFACTORING.md when present).
_validate_marker_invariants = validate_marker_invariants
_extract_user_tail = extract_user_tail
_interpolate_env = interpolate_env
_interpolate_metadata = interpolate_metadata


# Files that get marker-protected merge semantics (vs. full overwrite).
# Marker protection guards against silent data loss on a hand-edited file.
_MARKER_PROTECTED_BASENAMES = frozenset({"CLAUDE.md", "state.md"})

# Files DEEP-MERGED across the two-pass overlay (vs. full overwrite). The
# shared baseline ``_shared/to_home/.mcp.json`` carries the default MCP servers
# (sac / scitex-todo / claude-code-telegrammer); a per-agent ``.mcp.json`` must
# UNION its servers with that baseline, not replace it (which would silently
# drop the defaults). Same-name conflict → fail loud. See :func:`_deploy_mcp_merge`.
_MCP_MERGE_BASENAMES = frozenset({".mcp.json"})

# Files that get chmod 0600 after copy. ``.env`` only by default; the
# rest preserve source perms via ``shutil.copy2``.
_TIGHT_PERM_BASENAMES = frozenset({".env"})

# Files copied VERBATIM (no ${...} interpolation) + chmod 0600. ``.envrc`` is a
# shell script whose own ${VAR} / $(...) syntax must reach bash intact (sac
# interpolation would expand it at deploy time and corrupt the script); it is
# evaluated host-side post-deploy by :func:`_envrc.fold_envrc_into_env`.
_VERBATIM_SECRET_BASENAMES = frozenset({".envrc"})

# Env var: explicit override for the shared/common baseline to_home dir.
# Absolute path. When unset we fall back to ``<agents_dir>/_shared/to_home``
# (or the legacy ``_base`` sibling).
_BASELINE_ENV_VAR = "SAC_TO_HOME_BASELINE"

# Names of the sibling dir (under the agents root) that holds the common
# baseline. Agents live at ``<agents_dir>/<name>/``, so the agents root
# is the spec dir's parent and the baseline is ``<parent>/_shared/to_home``.
# ``_shared`` is the current name; ``_base`` is retained as a
# backward-compat fallback for hosts/fleets not yet renamed (first match
# under the agents root wins, in declared order).
_BASELINE_DIR_NAMES = ("_shared", "_base")


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
      2. ``<agents_dir>/_shared/to_home`` — a sibling ``_shared`` dir
         under the agents root (``_base`` accepted as a backward-compat
         fallback). Agents live at ``<agents_dir>/<name>/``, so the
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
    for name in _BASELINE_DIR_NAMES:
        p = spec_dir.parent / name / "to_home"
        if p.is_dir():
            return p
    return None


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
    # Credential-leak guard runs BEFORE any deploy (both layers).
    if baseline is not None:
        _scan_for_credential_leak(baseline)
    if root.is_dir():
        _scan_for_credential_leak(root)
    workspace_home.mkdir(parents=True, exist_ok=True)
    # Idempotency (see deploy_to_home): re-derive deep-merged files fresh so a
    # changed per-agent definition never false-conflicts with a stale copy.
    for _merge_name in _MCP_MERGE_BASENAMES:
        _stale = workspace_home / _merge_name
        if _stale.is_file():
            _stale.unlink()
    if baseline is not None:
        _walk_and_apply(baseline, baseline, workspace_home, config=None)
    if root.is_dir():
        _walk_and_apply(root, root, workspace_home, config=None)
    fold_envrc_into_env(workspace_home)


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
    # Credential-leak guard runs BEFORE any deploy (both layers).
    if baseline is not None:
        _scan_for_credential_leak(baseline)
    if root is not None:
        _scan_for_credential_leak(root)
    dest = Path(workspace_home)
    dest.mkdir(parents=True, exist_ok=True)
    # Idempotency: re-derive the deep-merged .mcp.json FRESH each deploy. Drop
    # any prior-deploy copy first so a CHANGED per-agent definition (e.g. an
    # updated telegrammer command/path) re-deploys cleanly, instead of
    # false-conflicting (McpMergeConflict) against last deploy's stale result
    # in _deploy_mcp_merge. No data loss — the file is regenerated below from
    # baseline ∪ per-agent.
    for _merge_name in _MCP_MERGE_BASENAMES:
        _stale = dest / _merge_name
        if _stale.is_file():
            _stale.unlink()
    if baseline is not None:
        _walk_and_apply(baseline, baseline, dest, config=config)
    if root is not None:
        _walk_and_apply(root, root, dest, config=config)
    # .envrc (if present) is a shell script: evaluate it host-side and fold
    # the result into dest/.env so build_run_argv's --env-file injects it.
    # No-op when the agent ships no .envrc.
    fold_envrc_into_env(dest)


# --- traversal -------------------------------------------------------------


def _scan_for_credential_leak(src_root: Path) -> None:
    """Raise :class:`WorkspaceCredentialLeakError` on first
    ``.credentials.json`` found at any depth under ``src_root``.

    Runs BEFORE any deploy so a rejected tree cannot land partial
    sibling content next to a leaked credential. See the error class
    docstring for the lead-reported incident this guards against.
    """
    if not src_root.is_dir():
        return
    for leak in src_root.rglob(".credentials.json"):
        rel = leak.relative_to(src_root)
        raise WorkspaceCredentialLeakError(
            f"to_home/ contains a .credentials.json at {rel} — refused. "
            "Credentials must come from the auth-stage rw bind, not "
            f"a static workspace copy. Remove {leak} and rely on "
            "spec.claude.account or spec.claude.credentials_file."
        )


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

    The caller (``materialize_to_home`` / ``deploy_to_home``) runs
    :func:`_scan_for_credential_leak` first so a leaked
    ``.credentials.json`` aborts the deploy before any sibling
    content lands.
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
        elif child.name in _MCP_MERGE_BASENAMES:
            _deploy_mcp_merge(child, dst, config=config, rel=rel)
        elif child.name in _TIGHT_PERM_BASENAMES:
            _deploy_tight_perm_file(child, dst, config=config, rel=rel)
        elif child.name in _VERBATIM_SECRET_BASENAMES:
            _deploy_verbatim_secret(child, dst, rel=rel)
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


def _deploy_mcp_merge(
    src: Path,
    dst: Path,
    *,
    config: AgentConfig | None,
    rel: Path,
) -> None:
    """Deep-merge ``.mcp.json`` with any already-deployed (baseline) copy.

    The two-pass overlay deploys the shared baseline ``.mcp.json`` first, then
    each agent's own lands here. Full-overwrite would silently drop the
    baseline's default servers (sac / scitex-todo / claude-code-telegrammer);
    instead we UNION the ``mcpServers`` (baseline ∪ per-agent) via
    :func:`_mcp_merge.merge_mcp_json`. Fail-loud (no silent fallback): an
    unparseable source/destination ``.mcp.json`` raises
    :class:`WorkspaceMcpMergeError`; a same-name server defined two different
    ways raises :class:`_mcp_merge.McpMergeConflict`. Both abort the deploy.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    _clear_readonly_dst(dst)
    overlay_text = _read_and_interpolate(src, config)
    try:
        overlay_doc = json.loads(overlay_text) if overlay_text.strip() else {}
    except json.JSONDecodeError as exc:
        raise WorkspaceMcpMergeError(
            f"to_home: source {rel} is not valid JSON ({exc})"
        ) from exc
    if dst.is_file():
        existing = dst.read_text()
        try:
            base_doc = json.loads(existing) if existing.strip() else {}
        except json.JSONDecodeError as exc:
            raise WorkspaceMcpMergeError(
                f"to_home: existing {dst} is not valid JSON ({exc})"
            ) from exc
        merged = merge_mcp_json(base_doc, overlay_doc)
    else:
        merged = overlay_doc
    dst.write_text(json.dumps(merged, indent=2) + "\n")
    logger.info("to_home: deep-merged .mcp.json %s -> %s", rel, dst)


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


def _deploy_verbatim_secret(src: Path, dst: Path, *, rel: Path) -> None:
    """Byte-for-byte copy (NO interpolation) + chmod 0600. For ``.envrc``.

    Unlike :func:`_deploy_tight_perm_file`, this NEVER runs ``${...}``
    interpolation: a ``.envrc`` is a shell script and its own ``${VAR}`` /
    ``$(...)`` syntax must reach bash intact (sac interpolation would expand
    it at deploy time and corrupt the script). The file is evaluated later by
    :func:`_envrc.fold_envrc_into_env`.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    _clear_readonly_dst(dst)
    shutil.copy2(src, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError as exc:  # stx-allow: fallback (reason: filesystem op failure)
        logger.warning("Failed to chmod 0600 on %s: %s", dst, exc)
    logger.info("to_home: deployed verbatim (0600) %s -> %s", rel, dst)


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
    # Preserve content AROUND a prior generated section: the ``head`` BEFORE it
    # (e.g. the setup_claude_md auto agent-section, which uses its OWN marker
    # style — so the two now compose instead of fatal-ing when the baseline
    # lives at .claude/CLAUDE.md) and the ``tail`` AFTER it (operator-appended
    # content). Malformed markers still fail loud inside the split.
    head, user_tail = split_around_generated_section(existing_text, str(dst))

    body = new_content
    if user_tail.strip():
        body = body.rstrip("\n") + user_tail
        if not body.endswith("\n"):
            body += "\n"
    if head.strip():
        updated = head.rstrip("\n") + "\n\n" + body
    else:
        updated = body

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
