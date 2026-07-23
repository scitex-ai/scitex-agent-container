"""Audit ``<workdir>/.claude/`` for the F-CS8 silent-failure footprint.

F-CS8 — claude-agent-sdk auto-discovers ``<workdir>/.claude/`` at session
start (hooks, skills, settings.local.json, agents). When that tree is
heavy — large bytes, but more importantly **many files** — the SDK either
times out spawning MCP servers or swallows discovery errors and returns
0 tokens per turn with no log line. The breakage observed in the field
was at ~42 k files where the bun MCP server silently never
spawned despite the `--mcp-config` listing it correctly. Smaller-fleet
peers at ~13 k files were skating on thin ice.

Two distinct leaks drove that failure, observed in the fleet sweep
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
with 1.1 GB / 2.7 k files (a single large-blob worktree), while the
failing agent's workdir was 884 MB / 41.8 k files. The walk cost
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
# it we have two confirmed silent failures (one agent at 41.8 k pre-clean,
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

    This narrow byte-only helper is preserved as the existing-test
    contract. The richer :func:`_summarize_gdu_json` extracts bytes,
    file count, AND per-subdir breakdown in one pass — that's what
    :func:`_measure_top_level` calls so gdu remains a single
    subprocess for the whole audit.
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


# ---------------------------------------------------------------------------
# Rich gdu-JSON extractor: bytes + files + per-subdir in ONE subprocess.
# This is what eliminates the trailing os.walk on hot paths — gdu has
# already walked every inode to compute its asize sum, so re-walking in
# Python just to count files is pure overhead. The 42 k-file
# pathology is the exact case this helps most.
# ---------------------------------------------------------------------------


def _try_gdu_summary(root: Path, *, curated_subdirs: tuple[str, ...]) -> dict | None:
    """Run ``gdu`` once, extract totals + per-curated-subdir breakdown.

    Returns ``{"bytes": int, "files": int, "per_subdir": dict}`` on
    success, ``None`` on any failure. ``per_subdir`` maps each curated
    ``rel_path`` (e.g. ``"worktrees"``) to a ``(bytes, files)`` tuple
    when that subdir exists under ``root``; missing subdirs are simply
    absent from the dict.

    Totals exclude any subtree whose dir basename matches
    :func:`scitex_agent_container._walk_exclusions.is_excluded_walk_dir`
    (currently ``worktrees``) so the worktrees bucket does not inflate
    the headline numbers. Per-subdir entries are NOT exclusion-aware —
    the bloat-source report intentionally surfaces worktrees so the
    operator can decide whether to prune it.

    Why one big gdu call without ``-I``: ``gdu -I '<regex>'`` excludes
    the directory from gdu's OWN scan, which means we lose the
    per-subdir bloat data. Doing the exclusion in the Python tree
    walk on gdu's JSON is O(N) over the JSON dict count — negligible
    next to gdu's syscall-bound walk — and lets the SAME gdu call
    fuel BOTH the totals (excluding worktrees) AND the bloat probe
    (which needs the worktrees bytes/files).
    """
    if shutil.which("gdu") is None:
        return None
    # stx-allow: fallback (reason: gdu may exit non-zero on permission
    # issues or timeout; caller logs the visible fallback)
    try:
        result = subprocess.run(
            ["gdu", "-o", "-", "--no-progress", str(root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):  # stx-allow: fallback
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    # stx-allow: fallback (reason: malformed JSON / unexpected schema
    # treated as tool-unusable; caller degrades to du+fd/walk)
    try:
        doc = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):  # stx-allow: fallback
        return None
    if not isinstance(doc, list) or len(doc) < 4:
        return None
    root_node = doc[3]
    if not isinstance(root_node, list):
        return None

    from ._walk_exclusions import is_excluded_walk_dir

    total_bytes, total_files = _sum_gdu_subtree(
        root_node, predicate=lambda name: not is_excluded_walk_dir(name)
    )
    per_subdir: dict[str, tuple[int, int]] = {}
    for rel in curated_subdirs:
        node = _navigate_gdu_path(root_node, rel.split("/"))
        if node is None:
            continue
        sb, sf = _sum_gdu_subtree(node, predicate=lambda _name: True)
        per_subdir[rel] = (sb, sf)
    return {"bytes": total_bytes, "files": total_files, "per_subdir": per_subdir}


def _sum_gdu_subtree(node: Any, *, predicate) -> tuple[int, int]:
    """Recursive ``(bytes, files)`` over a gdu node, skipping pruned dirs.

    ``predicate(basename) -> bool``: when ``False`` for a directory's
    basename, the entire subtree is skipped. Files (dicts) are always
    counted regardless of predicate (files don't have a "name to prune
    by directory rule" — predicate is dir-scoped).

    Non-regular files (gdu sets ``"notreg": true`` for symlinks /
    devices / sockets / FIFOs) are SKIPPED so the count matches the
    old ``os.walk`` semantics which explicitly filtered
    ``Path.is_symlink()`` to avoid following links into bound mounts
    or cyclic targets.
    """
    # File node (dict with asize). Skip non-regular files (gdu's
    # ``notreg`` covers symlinks + devices + sockets + FIFOs).
    if isinstance(node, dict):
        if node.get("notreg") is True:
            return 0, 0
        asize = node.get("asize")
        bytes_here = asize if isinstance(asize, int) else 0
        files_here = 1 if isinstance(asize, int) else 0
        return bytes_here, files_here
    # Directory node (list starting with a meta dict).
    if not isinstance(node, list) or not node:
        return 0, 0
    meta = node[0]
    if isinstance(meta, dict):
        name = meta.get("name")
        if isinstance(name, str) and not predicate(name):
            return 0, 0
    total_bytes = 0
    total_files = 0
    for child in node[1:]:
        cb, cf = _sum_gdu_subtree(child, predicate=predicate)
        total_bytes += cb
        total_files += cf
    return total_bytes, total_files


def _navigate_gdu_path(root_node: Any, components: list[str]) -> Any | None:
    """Descend gdu directory-list nodes by ``components`` (path segments).

    Returns the matching child node (list for dir, dict for file) or
    ``None`` if any path segment is missing. ``components`` of length
    zero returns ``root_node`` itself.
    """
    if not components:
        return root_node
    if not isinstance(root_node, list) or len(root_node) < 2:
        return None
    head = components[0]
    rest = components[1:]
    for child in root_node[1:]:
        if isinstance(child, list) and child and isinstance(child[0], dict):
            if child[0].get("name") == head:
                return _navigate_gdu_path(child, rest)
        # File-leaf: only matches if it's the final component.
        elif isinstance(child, dict) and not rest:
            if child.get("name") == head:
                return child
    return None


def _try_fd_file_count(root: Path) -> int | None:
    """Try ``fd`` for a fast file count under ``root``, excluding worktrees.

    Returns the count on success, ``None`` on any failure (binary
    missing, non-zero exit). Used in Tier 2 when gdu is absent but
    fd-find IS present (common Ubuntu CI runner state).

    ``fd`` is preferred over a Python walk for counting because it is
    parallel, gitignore-aware-off (`--no-ignore`), and emits one path
    per line — `sum(1 for _ in lines)` is the entire parser. Note the
    Ubuntu package name is ``fd-find`` and the binary is named ``fd``
    on most distros but ``fdfind`` on Debian; this code looks for
    ``fd`` first, then ``fdfind``, for portability.
    """
    binary = shutil.which("fd") or shutil.which("fdfind")
    if binary is None:
        return None
    # stx-allow: fallback (reason: fd may exit non-zero on permission
    # denied entries; caller logs visible fallback)
    try:
        result = subprocess.run(
            [
                binary,
                "--type",
                "f",
                "--hidden",
                "--no-ignore",
                "--exclude",
                "worktrees",
                ".",
                str(root),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):  # stx-allow: fallback
        return None
    if result.returncode != 0:
        return None
    return sum(1 for line in result.stdout.splitlines() if line.strip())


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
    root: Path,
    *,
    file_threshold: int,
    byte_threshold: int,
    curated_subdirs: tuple[str, ...] = (),
) -> tuple[int, int, bool, dict[str, tuple[int, int]] | None]:
    """Measure ``root`` (excluding ``worktrees/``) via the fastest tool present.

    The F-CS8 audit only needs "is it heavy? yes/no" — not the exact
    total. We measure via a three-tier chain, with each fallback
    transition LOGGED at WARNING level so the operator sees the
    degraded path (the no-silent-fallback discipline; ywatanabe core
    rule). Tiers, fastest first:

      1. ``gdu -o -`` (machine-readable JSON). One subprocess yields
         bytes + file count + per-curated-subdir breakdown via a
         single Python-side tree walk over gdu's JSON (no second
         os.walk on disk). This is the only tier the SIF will see in
         production. See :func:`_try_gdu_summary` for the parser's
         version contract.
      2. ``du -sb --exclude=worktrees`` for bytes + ``fd`` (fd-find,
         ``--exclude worktrees -t f -H --no-ignore``) for file count.
         Two subprocesses but both are fast and stable. Used on CI
         runners and dev hosts that have du+fd but not the SIF-pinned
         gdu. Per-subdir breakdown returns ``None`` and the caller
         falls back to a small targeted ``os.walk`` PER CURATED
         SUBDIR (worktrees/, .pending/) — bounded, not over the full
         tree.
      3. Bounded Python ``os.walk`` with shared prune + EARLY-EXIT
         once EITHER threshold is crossed. O(threshold), not
         O(tree). The last-resort fallback when both gdu AND fd
         are missing.

    Returns ``(files, bytes, early_exit, per_subdir_or_None)``:

    * If gdu Tier 1 succeeds: ``per_subdir`` is a dict mapping each
      ``curated_subdirs`` entry that exists under ``root`` to a
      ``(bytes, files)`` tuple. ``early_exit`` is always ``False``
      because gdu produces exact totals in one fast call.
    * Otherwise: ``per_subdir`` is ``None`` and the caller must
      probe curated subdirs separately.
    * In Tier 3, ``early_exit=True`` means at least one threshold
      was crossed; ``files`` / ``bytes`` are lower bounds at exit.

    Visible-fallback warnings:

    * ``gdu`` not found / failed → ``logger.warning(...)``.
    * ``fd`` (or ``fdfind``) not found / failed when du-byte tier
      ran → ``logger.warning(...)`` and fall through to os.walk.
    * ``du`` not found / failed → ``logger.warning(...)`` and fall
      through to os.walk.
    """
    from ._walk_exclusions import prune_walk_dirnames

    if not root.is_dir():
        return 0, 0, False, None

    # ---- Tier 1: gdu (JSON; bytes + files + per-subdir in one call) --------
    if shutil.which("gdu") is None:
        logger.warning(
            "gdu not found; falling back to du+fd for .claude audit "
            "(slower on large trees). No-silent-fallback discipline: "
            "this warning fires on every audit until gdu is on PATH. "
            "The SIF's apptainer-base.def is expected to bake gdu in."
        )
    else:
        summary = _try_gdu_summary(root, curated_subdirs=curated_subdirs)
        if summary is None:
            logger.warning(
                "gdu invocation failed for .claude audit; falling "
                "back to du+fd. Verify the gdu version pinned in the "
                "SIF still matches the JSON-schema contract in "
                "_try_gdu_summary() (gdu major version bump = "
                "re-verify parser)."
            )
        else:
            return (
                summary["files"],
                summary["bytes"],
                False,
                summary["per_subdir"],
            )

    # ---- Tier 2a: du for bytes ---------------------------------------------
    fast_bytes: int | None = None
    if shutil.which("du") is None:
        logger.warning(
            "du not found; using bounded os.walk for .claude byte "
            "audit (slowest tier). No-silent-fallback discipline: "
            "this warning fires on every audit until du is on "
            "PATH. Install coreutils to restore the fast path."
        )
    else:
        fast_bytes = _try_du_bytes(root)
        if fast_bytes is None:
            logger.warning(
                "du invocation failed for .claude byte audit; "
                "falling back to bounded os.walk. Check that du "
                "supports `-sb --exclude=<pat>` on this host."
            )

    # ---- Tier 2b: fd for file count ----------------------------------------
    fast_files: int | None = None
    if shutil.which("fd") is None and shutil.which("fdfind") is None:
        logger.warning(
            "fd (fd-find) not found; using bounded os.walk for "
            ".claude file-count audit. Install fd-find for a fast "
            "parallel count; this warning fires until either fd or "
            "gdu is on PATH."
        )
    else:
        fast_files = _try_fd_file_count(root)
        if fast_files is None:
            logger.warning(
                "fd invocation failed for .claude file-count audit; "
                "falling back to bounded os.walk. Check fd flag "
                "support on this host."
            )

    # ---- Short-circuit: both Tier 2 tools returned ------------------------
    # If du AND fd both produced numbers, we are DONE — no need for the
    # bounded os.walk just to re-confirm. The caller will compute
    # per-subdir bloat via small targeted walks.
    if fast_bytes is not None and fast_files is not None:
        return fast_files, fast_bytes, False, None

    # ---- Tier 3: bounded Python walk with early-exit -----------------------
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
                    total_bytes += fpath.stat().st_size
                    effective_bytes = (
                        fast_bytes if fast_bytes is not None else total_bytes
                    )
                    effective_files = (
                        fast_files if fast_files is not None else total_files
                    )
                    if (
                        effective_files > file_threshold
                        or effective_bytes > byte_threshold
                    ):
                        early_exit = True
                        return (
                            effective_files,
                            effective_bytes,
                            True,
                            None,
                        )
            except OSError:  # stx-allow: fallback (reason: see inline comment)
                continue

    final_bytes = fast_bytes if fast_bytes is not None else total_bytes
    final_files = fast_files if fast_files is not None else total_files
    return final_files, final_bytes, early_exit, None


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
    subdirs = tuple(probed_subdirs) if probed_subdirs is not None else _PROBED_SUBDIRS
    total_files, total_bytes, _early_exit, per_subdir_map = _measure_top_level(
        root,
        file_threshold=file_threshold,
        byte_threshold=byte_threshold,
        curated_subdirs=subdirs,
    )

    bloat_threshold = bloat_subdir_threshold_files()
    bloat: list[SubdirAudit] = []
    if per_subdir_map is not None:
        # Tier 1 (gdu) gave us everything in one call — no extra walks.
        for rel, (b_bytes, b_files) in per_subdir_map.items():
            if b_files >= bloat_threshold:
                bloat.append(SubdirAudit(rel_path=rel, files=b_files, bytes=b_bytes))
    else:
        # Tier 2/3 fallback — small targeted walks per curated subdir.
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
