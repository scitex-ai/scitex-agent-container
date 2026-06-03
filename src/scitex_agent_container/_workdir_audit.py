"""Audit ``<workdir>/.claude/`` for the F-CS8 silent-failure footprint.

F-CS8 — claude-agent-sdk auto-discovers ``<workdir>/.claude/`` at session
start (hooks, skills, settings.local.json, agents). When that tree is
heavy — large bytes, but more importantly **many files** — the SDK either
times out spawning MCP servers or swallows discovery errors and returns
0 tokens per turn with no log line. The breakage observed in the field
(orochi) was at ~42 k files where the bun MCP server silently never
spawned despite the `--mcp-config` listing it correctly. Smaller-fleet
peers at ~13 k files were skating on thin ice.

Two distinct leaks drove the orochi failure, observed in the fleet sweep
2026-06-03:

1. ``.claude/worktrees/agent-*`` — subagent worktrees accumulate; the
   ephemeral subagent design never includes cleanup. Each worktree is
   a full git checkout, multiplying the directory walk linearly with
   subagent history.

2. ``.claude/hooks/pre-tool-use/.pending/toolu_*.json`` — pipe-stage
   permission records written by ``pipe-stage-permissions.sh`` per
   gated tool-use call, never cleaned. Two agents in the fleet had >
   5 k records each, single-directory.

Pure file-count is the dominant signal: ``proj-grant`` runs healthy
with 1.1 GB / 2.7 k files (a single large-blob worktree), while
``proj-scitex-orochi`` failed at 884 MB / 41.8 k files. The walk cost
scales with file count, not bytes.

This module exposes a pure-function audit + structured result so:

* ``runtimes/claude_session._warn_if_heavy_workdir_claude`` can surface
  a LOUD, actionable warning that points at the actual bloat-source
  subdir (PR-A:b);
* ``cli_pkg/status_cmds.sac agents status --workdir-audit`` can surface
  the count for at-a-glance fleet inspection without operators having
  to ``find … | wc -l`` themselves (PR-A:a);
* ``cli_pkg/_workdir_prune`` can drive a deterministic prune with the
  same bloat-source detection (PR-A:c).

Never raises. ``OSError`` (permission-denied, broken symlink) and
non-existent ``<workdir>/.claude/`` collapse to zero contribution. The
audit walks the filesystem at call time — there is no caching, no
background materialisation. Callers that want a cached value layer
their own cache on top.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Default thresholds (env-overridable)
# ---------------------------------------------------------------------------
#
# The byte threshold is the historical F-CS8 trip (10 MiB). The file-count
# threshold (5 000) is the lead-confirmed level from the 2026-06-03 fleet
# sweep — below this every observed fleet member has live MCP tools; above
# it we have two confirmed silent failures (orochi at 41.8 k pre-clean,
# my own agent at 13.4 k post-bloat-discovery). 5 k is conservative.
#
# The per-subdir threshold (1 000) catches the specific bloat-source
# subdirectories so the warning can point AT the leak — ``.pending/`` or
# ``.claude/worktrees/`` rather than the unhelpful tree-total.

_DEFAULT_BYTES_THRESHOLD = 10 * 1024 * 1024  # 10 MiB
_DEFAULT_FILE_COUNT_THRESHOLD = 5_000
_DEFAULT_BLOAT_SUBDIR_FILE_THRESHOLD = 1_000

_ENV_BYTES = "SAC_WORKDIR_CLAUDE_WARN_BYTES"
_ENV_FILES = "SAC_WORKDIR_CLAUDE_WARN_FILES"
_ENV_BLOAT_SUBDIR_FILES = "SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES"


def _resolve_int_env(env_name: str, default: int) -> int:
    """Return positive int from ``$env_name``; ``default`` on missing/invalid."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def warn_threshold_bytes() -> int:
    """Resolve the byte-size warn threshold at call time."""
    return _resolve_int_env(_ENV_BYTES, _DEFAULT_BYTES_THRESHOLD)


def warn_threshold_files() -> int:
    """Resolve the file-count warn threshold at call time."""
    return _resolve_int_env(_ENV_FILES, _DEFAULT_FILE_COUNT_THRESHOLD)


def bloat_subdir_threshold_files() -> int:
    """Resolve the per-subdir file-count bloat threshold at call time."""
    return _resolve_int_env(
        _ENV_BLOAT_SUBDIR_FILES, _DEFAULT_BLOAT_SUBDIR_FILE_THRESHOLD
    )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubdirAudit:
    """File count + byte size for one subdirectory under ``<workdir>/.claude/``.

    ``rel_path`` is relative to ``<workdir>/.claude/`` (e.g. ``"worktrees"`` or
    ``"hooks/pre-tool-use/.pending"``). Keeping it relative makes the warn
    output portable across hosts and copy-pasteable into ``mv`` commands.
    """

    rel_path: str
    files: int
    bytes: int


@dataclass(frozen=True)
class WorkdirClaudeAudit:
    """Aggregated audit of ``<workdir>/.claude/``.

    * ``workdir`` — input as-given (caller-relative; useful in warn output).
    * ``files`` / ``bytes`` — totals across the whole tree.
    * ``bloat_sources`` — per-subdir entries whose file count exceeds
      :func:`bloat_subdir_threshold_files`. Ordered by file count descending
      so the worst offender is first.
    * ``exceeded_files`` / ``exceeded_bytes`` — convenience booleans against
      the configured thresholds; cheap to compute and stable surface for
      external consumers (status JSON, prune CLI, start-hook warn).
    * ``threshold_files`` / ``threshold_bytes`` — echo the resolved
      thresholds so warn output can quote them without re-resolving the env.
    * ``missing`` — ``True`` iff ``<workdir>/.claude/`` does not exist or is
      not a directory; the rest of the fields are zero in that case.
    """

    workdir: str
    files: int
    bytes: int
    bloat_sources: tuple[SubdirAudit, ...]
    exceeded_files: bool
    exceeded_bytes: bool
    threshold_files: int
    threshold_bytes: int
    missing: bool = False


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------


# Subdirectories we explicitly probe for per-bucket bloat. Keeping the list
# small + curated avoids reporting noise for every random sub-tree. Add to
# this list as new bloat sources are observed in the wild — the warn text
# is generic over rel_path so no other change is needed.
_PROBED_SUBDIRS: tuple[str, ...] = (
    "worktrees",
    "hooks/pre-tool-use/.pending",
)


def _walk_size_and_count(root: Path) -> tuple[int, int]:
    """Return ``(files, bytes)`` for ``root`` (recursive, no symlinks).

    Symlinks are deliberately NOT followed — they can create cycles and
    they don't mirror how claude-agent-sdk walks its discovery tree.
    Failed stat()s contribute zero, matching the F-CS8 pre-flight
    behaviour the warn path already uses. Never raises.
    """
    total_bytes = 0
    total_files = 0
    if not root.is_dir():
        return 0, 0
    for path in root.rglob("*"):
        # stx-allow: fallback (reason: stat may fail on broken symlinks or
        # permission-denied entries; treat as 0 bytes / not-counted rather
        # than abort the whole audit — matches existing F-CS8 pre-flight)
        try:
            if path.is_symlink():
                continue
            if path.is_file():
                total_files += 1
                total_bytes += path.stat().st_size
        except OSError:  # stx-allow: fallback (reason: see inline comment)
            continue
    return total_files, total_bytes


def _probe_subdir(root: Path, rel: str) -> SubdirAudit | None:
    """Audit one curated subdir under ``root``; ``None`` if it doesn't exist."""
    subdir = root / rel
    if not subdir.is_dir():
        return None
    files, byte_count = _walk_size_and_count(subdir)
    return SubdirAudit(rel_path=rel, files=files, bytes=byte_count)


def audit_workdir_claude(
    workdir: str | os.PathLike[str] | None,
    *,
    probed_subdirs: Iterable[str] | None = None,
) -> WorkdirClaudeAudit:
    """Walk ``<workdir>/.claude/`` and return a structured audit.

    Args:
        workdir: The workdir to audit. ``None``, empty, or a path with no
            ``.claude/`` subtree returns a zero-valued ``missing=True`` audit.
        probed_subdirs: Override the curated list of subdirs probed for
            per-bucket bloat reporting. Defaults to ``_PROBED_SUBDIRS``.

    Returns:
        :class:`WorkdirClaudeAudit`. Never raises.

    The walk is single-pass over the tree (one ``rglob`` for totals); per-
    subdir audits walk only the curated bloat-source subtrees, so probing
    cost stays O(bloat-source-files), not O(tree).
    """
    workdir_str = "" if workdir is None else str(workdir)
    if not workdir_str:
        return WorkdirClaudeAudit(
            workdir="",
            files=0,
            bytes=0,
            bloat_sources=(),
            exceeded_files=False,
            exceeded_bytes=False,
            threshold_files=warn_threshold_files(),
            threshold_bytes=warn_threshold_bytes(),
            missing=True,
        )

    root = Path(workdir_str) / ".claude"
    if not root.is_dir():
        return WorkdirClaudeAudit(
            workdir=workdir_str,
            files=0,
            bytes=0,
            bloat_sources=(),
            exceeded_files=False,
            exceeded_bytes=False,
            threshold_files=warn_threshold_files(),
            threshold_bytes=warn_threshold_bytes(),
            missing=True,
        )

    total_files, total_bytes = _walk_size_and_count(root)

    subdirs = tuple(probed_subdirs) if probed_subdirs is not None else _PROBED_SUBDIRS
    bloat_threshold = bloat_subdir_threshold_files()
    bloat: list[SubdirAudit] = []
    for rel in subdirs:
        sub = _probe_subdir(root, rel)
        if sub is not None and sub.files >= bloat_threshold:
            bloat.append(sub)
    bloat.sort(key=lambda s: s.files, reverse=True)

    return WorkdirClaudeAudit(
        workdir=workdir_str,
        files=total_files,
        bytes=total_bytes,
        bloat_sources=tuple(bloat),
        exceeded_files=total_files > warn_threshold_files(),
        exceeded_bytes=total_bytes > warn_threshold_bytes(),
        threshold_files=warn_threshold_files(),
        threshold_bytes=warn_threshold_bytes(),
        missing=False,
    )


# ---------------------------------------------------------------------------
# JSON projection
# ---------------------------------------------------------------------------


def to_dict(audit: WorkdirClaudeAudit) -> dict:
    """Project an audit to a plain dict suitable for ``json.dumps``.

    ``bloat_sources`` becomes a list of ``{rel_path, files, bytes}`` so the
    consumer can sort/render however it likes. Field names match the
    dataclass attributes so downstream code can shift to dataclass-direct
    access without renaming on the wire.
    """
    return {
        "workdir": audit.workdir,
        "files": audit.files,
        "bytes": audit.bytes,
        "bloat_sources": [
            {"rel_path": s.rel_path, "files": s.files, "bytes": s.bytes}
            for s in audit.bloat_sources
        ],
        "exceeded_files": audit.exceeded_files,
        "exceeded_bytes": audit.exceeded_bytes,
        "threshold_files": audit.threshold_files,
        "threshold_bytes": audit.threshold_bytes,
        "missing": audit.missing,
    }


__all__ = [
    "SubdirAudit",
    "WorkdirClaudeAudit",
    "audit_workdir_claude",
    "bloat_subdir_threshold_files",
    "to_dict",
    "warn_threshold_bytes",
    "warn_threshold_files",
]
