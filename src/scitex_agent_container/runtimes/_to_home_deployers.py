"""Per-entry deploy helpers for ``to_home`` materialization.

Extracted from :mod:`._to_home` (which had grown past the line cap), mirroring
the existing ``_to_home_text`` / ``_to_home_settings`` / ``_to_home_errors``
extractions. These are the low-level "deploy ONE source entry to ONE
destination" primitives the traversal in :mod:`._to_home` dispatches to per
basename class:

  - :func:`_deploy_plain_file` — full overwrite.
  - :func:`_deploy_mcp_merge` — deep-merge ``.mcp.json`` (baseline ∪ per-agent).
  - :func:`_deploy_tight_perm_file` — overwrite + chmod 0600 (``.env``).
  - :func:`_deploy_verbatim_secret` — byte copy, NO interpolation, 0600 (``.envrc``).
  - :func:`_deploy_marker_protected` — marker-protected merge (CLAUDE.md / state.md).

:mod:`._to_home` re-exports these so legacy import paths
(``from ...runtimes._to_home import _deploy_plain_file``) keep resolving.
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
from ._mcp_merge import merge_mcp_json
from ._to_home_errors import WorkspaceMcpMergeError
from ._to_home_text import (
    END_MARKER,
    interpolate_env,
    interpolate_metadata,
    split_around_generated_section,
)

logger = logging.getLogger(__name__)


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
        text = interpolate_metadata(text, config)
        text = interpolate_env(text)
    return text


def _dst_resolves_to_source(src: Path, dst: Path) -> bool:
    """True when ``dst`` already refers to the SAME file as ``src``.

    Happens when a prior deploy materialized this entry as a "linked host
    file" (a symlink into ``~/.claude``), or via a hardlink/bind.
    ``Path.samefile`` follows symlinks and compares device+inode; a missing
    or broken ``dst`` returns ``False`` so the deploy proceeds normally.
    """
    try:
        return dst.exists() and src.samefile(dst)
    except OSError:  # stx-allow: fallback (unreadable/broken dst → treat as not-same, let deploy proceed)
        return False


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
    # If dst ALREADY resolves to src (a prior "linked host file" symlink, a
    # hardlink, or a bind), the copy is a no-op — and worse, writing/interpolating
    # would follow the link and CORRUPT the shared host source. Skip cleanly;
    # ``shutil.copy2`` would otherwise raise ``SameFileError``. (INCIDENT
    # 2026-07-02: ``sac agents restart neurovista`` failed on
    # ``~/.claude/commands/autonomous.md`` — dst was a symlink back to src.)
    if _dst_resolves_to_source(src, dst):
        logger.info("to_home: %s already resolves to source; skip", rel)
        return
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
        section_content = interpolate_metadata(section_content, config)

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


__all__ = [
    "_clear_readonly_dst",
    "_deploy_marker_protected",
    "_deploy_mcp_merge",
    "_deploy_plain_file",
    "_deploy_tight_perm_file",
    "_deploy_verbatim_secret",
    "_read_and_interpolate",
]
