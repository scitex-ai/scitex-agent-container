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

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

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
    """Return EXACT ``(files, bytes)`` for ``root`` (recursive, no symlinks).

    Used by :func:`_probe_subdir` for per-bucket bloat reporting where
    the operator wants the precise count of a specific known-bloat
    subdir (``worktrees``, ``.pending``, ...). No early-exit because
    the probe is already targeted at a bounded, small-N location.

    Symlinks are deliberately NOT followed — they can create cycles and
    they don't mirror how claude-agent-sdk walks its discovery tree.
    Failed stat()s contribute zero. Never raises.

    Excluded subtrees (see :mod:`_walk_exclusions`) are PRUNED — most
    importantly ``worktrees/`` directories anywhere in the walk. The
    probe in :func:`_probe_subdir` is unaffected because the prune is
    BASENAME-keyed: a walk rooted INSIDE a ``worktrees/`` directory
    sees ``agent-*`` entries (none matching the exclusion), so per-
    bucket telemetry still descends.
    """
    from ._walk_exclusions import prune_walk_dirnames

    total_bytes = 0
    total_files = 0
    if not root.is_dir():
        return 0, 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune in place so os.walk does not descend the excluded subtrees.
        prune_walk_dirnames(dirnames)
        for fname in filenames:
            fpath = Path(dirpath) / fname
            # stx-allow: fallback (reason: stat may fail on broken symlinks
            # or permission-denied entries; treat as 0 bytes / not-counted
            # rather than abort the whole audit — matches existing F-CS8
            # pre-flight)
            try:
                if fpath.is_symlink():
                    continue
                if fpath.is_file():
                    total_files += 1
                    total_bytes += fpath.stat().st_size
            except OSError:  # stx-allow: fallback (reason: see inline comment)
                continue
    return total_files, total_bytes


def _try_gdu_bytes(root: Path) -> int | None:
    """Try ``gdu`` for the byte total (excluding ``worktrees/``) via its
    machine-readable JSON export.

    Returns the integer apparent-size byte total on success, ``None``
    on any failure (binary missing, non-zero exit, unparseable JSON,
    unexpected schema). Caller logs the fallback transition; this
    function stays quiet so the caller controls the chain narrative.

    Why gdu (parallel disk-usage scanner): 3-10x faster than ``du`` on
    large trees. We use ``-o -`` (JSON to stdout) instead of the
    human-readable ``--summarize`` output — JSON's schema is stable
    across gdu releases on the same major version, eliminating the
    per-release human-format parse drift that's caused gdu adoption
    to stall elsewhere.

    PARSER VERSION CONTRACT:
      Tied to ``gdu`` 5.x (as pinned in the SIF's apptainer-base.def).
      The JSON shape is::

        [ <format-version>, <schema-version>, {header}, <root-node> ]

      where each <node> is either:
        * A directory: ``[ {folder-meta-dict}, <child1>, <child2>, ... ]``
        * A file:      ``{ "name": ..., "asize": <bytes>, "dsize": <disk>, ... }``

      We recurse and sum the ``asize`` (apparent size — file st_size
      sum, matching the Python walk's semantics for the existing
      per-bucket probe contract).

      If ``.def`` bumps gdu to a future major version (6.x+), re-verify
      this parser by spot-checking ``gdu -o-`` output structure
      against a known fixture; bump the version comment here.

    ``-I ".*/worktrees"`` excludes any directory whose absolute path
    ends with ``/worktrees`` at any depth. Verified on this host.
    """
    if shutil.which("gdu") is None:
        return None
    # stx-allow: fallback (reason: gdu may exit non-zero on permission
    # issues or timeout; caller logs the visible fallback)
    try:
        result = subprocess.run(
            [
                "gdu",
                "-o",
                "-",
                "--no-progress",
                "-I",
                ".*/worktrees",
                str(root),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):  # stx-allow: fallback
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return _sum_asize_from_gdu_json(result.stdout)


def _sum_asize_from_gdu_json(blob: str) -> int | None:
    """Parse gdu's JSON export and sum ``asize`` recursively.

    Returns the integer total, or ``None`` if parsing fails. See
    :func:`_try_gdu_bytes` for the schema contract.
    """
    # stx-allow: fallback (reason: malformed JSON or unexpected schema
    # version is treated as "tool unusable" so the caller can degrade)
    try:
        doc = json.loads(blob)
    except (json.JSONDecodeError, ValueError):  # stx-allow: fallback
        return None
    if not isinstance(doc, list) or len(doc) < 4:
        return None
    return _walk_gdu_node_for_asize(doc[3])


def _walk_gdu_node_for_asize(node: Any) -> int:
    """Recursively sum ``asize`` across a gdu JSON node.

    File nodes are dicts with ``asize``; directory nodes are lists
    where index 0 is the directory's metadata dict (no asize) and
    indices 1.. are the children. Unknown shapes contribute 0
    (defense in depth).
    """
    if isinstance(node, dict):
        size = node.get("asize")
        return size if isinstance(size, int) else 0
    if isinstance(node, list):
        if not node:
            return 0
        # The first entry is the folder's own metadata (dir, no size);
        # subsequent entries are the children to recurse into.
        return sum(_walk_gdu_node_for_asize(child) for child in node[1:])
    return 0


def _try_du_bytes(root: Path) -> int | None:
    """Try ``du -sb --exclude=worktrees`` for the byte total.

    Returns the byte count on success, ``None`` on any failure (binary
    missing, non-zero exit, unparseable output). Caller logs the
    fallback transition; this function stays quiet so the caller
    controls the chain narrative.

    Why ``du`` and not ``gdu``: gdu (dundee/gdu) is faster but its
    CLI output format varies per release (human units in the
    ``--summarize`` line: "1.2 MiB", "512 B"), and the
    ``--ignore-dirs-pattern`` is an ABSOLUTE-PATH regex (the bare
    basename ``worktrees`` doesn't match — you need ``.*/worktrees``,
    which is itself version-fragile). ``du -sb --exclude=worktrees``
    is universally present and emits a stable ``<bytes>\\t<path>``
    line that needs no parsing logic that can drift. Lead-confirmed
    2026-06-04: du is the right call for the programmatic audit.

    Note that ``du -sb`` is APPARENT size INCLUDING directory entries
    (each typically 4 KiB on ext4) and INCLUDING disk-block padding
    for small files (1-byte files allocate full 4 KiB blocks). That
    inflates the byte count relative to the file-st_size sum the
    Python walk produces. Acceptable per the lead's directive
    ("don't compute the exact total — the audit only needs 'is it
    heavy? yes/no'") AND because the bumped byte threshold is a
    HEURISTIC for "tree is heavy enough to slow boot", which the
    du semantics actually capture better (disk usage IS the cost
    surface).
    """
    if shutil.which("du") is None:
        return None
    # stx-allow: fallback (reason: du may exit non-zero on
    # permission-denied entries or timeout on NFS; caller logs the
    # visible fallback)
    try:
        result = subprocess.run(
            ["du", "-sb", "--exclude=worktrees", str(root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):  # stx-allow: fallback
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    first_line = result.stdout.split("\n", 1)[0]
    bytes_str = (
        first_line.split("\t", 1)[0]
        if "\t" in first_line
        else first_line.split(None, 1)[0]
    )
    try:
        return int(bytes_str)
    except ValueError:
        return None


def _measure_top_level(
    root: Path, *, file_threshold: int, byte_threshold: int
) -> tuple[int, int, bool]:
    """Threshold-aware measurement of ``root`` (excluding ``worktrees/``).

    The F-CS8 audit only needs "is it heavy? yes/no" — not the exact
    total. We measure via a three-tier chain, with each fallback
    transition LOGGED at WARNING level so the operator sees the
    degraded path (the no-silent-fallback discipline; ywatanabe core
    rule). Tiers, fastest first:

      1. ``gdu -o -`` (machine-readable JSON, exclude regex). Fastest
         on large trees and parses cleanly because gdu's JSON schema
         is stable per major version (the SIF pins gdu's version in
         the .def so we own the schema). See :func:`_try_gdu_bytes`
         for the parser's version contract.
      2. ``du -sb --exclude=worktrees`` — universal Linux tool, stable
         ``<bytes>\\t<path>`` output. Fallback when gdu is absent or
         fails.
      3. Bounded Python ``os.walk`` with shared prune + EARLY-EXIT
         once EITHER threshold is crossed. O(threshold), not
         O(tree). The last-resort fallback that always works.

    Files count always comes from the Python walk (gdu/du don't
    cheaply report file counts), but the walk stays bounded by the
    same early-exit gate.

    Returns ``(files, bytes, early_exit)``:

    * If ``early_exit`` is ``True``: at least one threshold was
      crossed; ``files`` / ``bytes`` are LOWER BOUNDS at exit. The
      threshold that triggered exit is definitively crossed; the
      other's exceeded-ness is unknown and treated as
      not-exceeded (the actionable alarm has already fired).
    * If ``early_exit`` is ``False``: the walk completed; the
      Python ``total_files`` is exact, and ``bytes`` is whichever
      tier produced an answer (gdu > du > Python walk).

    Visible-fallback warnings:

    * ``gdu`` not found → ``logger.warning(...)`` + try ``du``.
    * ``gdu`` found but failed → ``logger.warning(...)`` + try ``du``.
    * ``du`` not found → ``logger.warning(...)`` + use Python walk.
    * ``du`` found but failed → ``logger.warning(...)`` + use Python walk.
    """
    from ._walk_exclusions import prune_walk_dirnames

    if not root.is_dir():
        return 0, 0, False

    # ---- Tier 1: gdu (JSON) -------------------------------------------------
    fast_bytes: int | None = None
    if shutil.which("gdu") is None:
        logger.warning(
            "gdu not found; falling back to du for .claude size "
            "audit (slower on large trees). No-silent-fallback "
            "discipline: this warning fires on every audit until "
            "gdu is on PATH. The SIF's apptainer-base.def is "
            "expected to bake gdu in."
        )
    else:
        fast_bytes = _try_gdu_bytes(root)
        if fast_bytes is None:
            logger.warning(
                "gdu invocation failed for .claude size audit; "
                "falling back to du. Verify the gdu version pinned "
                "in the SIF still matches the JSON-schema contract "
                "in _try_gdu_bytes() (gdu major version bump = "
                "re-verify parser)."
            )

    # ---- Tier 2: du ---------------------------------------------------------
    if fast_bytes is None:
        if shutil.which("du") is None:
            logger.warning(
                "du not found; using bounded os.walk for .claude size "
                "audit (slowest tier). No-silent-fallback discipline: "
                "this warning fires on every audit until du is on "
                "PATH. Install coreutils to restore the fast path."
            )
        else:
            fast_bytes = _try_du_bytes(root)
            if fast_bytes is None:
                logger.warning(
                    "du invocation failed for .claude size audit; "
                    "falling back to bounded os.walk. Check that du "
                    "supports `-sb --exclude=<pat>` on this host."
                )

    # ---- Tier 2 (and file count): bounded Python walk with early-exit -----
    total_bytes = 0
    total_files = 0
    early_exit = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        prune_walk_dirnames(dirnames)
        for fname in filenames:
            fpath = Path(dirpath) / fname
            # stx-allow: fallback (reason: stat may fail on broken symlinks
            # or permission-denied entries; treat as 0 bytes / not-counted)
            try:
                if fpath.is_symlink():
                    continue
                if fpath.is_file():
                    total_files += 1
                    # When an external tier produced fast_bytes we still
                    # track Python's own running byte total so the walk
                    # remains threshold-aware even when fast_bytes is
                    # < byte_threshold (the threshold check uses
                    # fast_bytes when available, else total_bytes).
                    total_bytes += fpath.stat().st_size
                    effective_bytes = (
                        fast_bytes if fast_bytes is not None else total_bytes
                    )
                    if total_files > file_threshold or effective_bytes > byte_threshold:
                        early_exit = True
                        return (
                            total_files,
                            effective_bytes,
                            True,
                        )
            except OSError:  # stx-allow: fallback (reason: see inline comment)
                continue

    final_bytes = fast_bytes if fast_bytes is not None else total_bytes
    return total_files, final_bytes, early_exit


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

    file_threshold = warn_threshold_files()
    byte_threshold = warn_threshold_bytes()
    total_files, total_bytes, _early_exit = _measure_top_level(
        root, file_threshold=file_threshold, byte_threshold=byte_threshold
    )

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
        exceeded_files=total_files > file_threshold,
        exceeded_bytes=total_bytes > byte_threshold,
        threshold_files=file_threshold,
        threshold_bytes=byte_threshold,
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
