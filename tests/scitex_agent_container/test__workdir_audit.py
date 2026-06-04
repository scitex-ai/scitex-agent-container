"""Tests for :mod:`scitex_agent_container._workdir_audit`.

Real I/O against tmp fixture trees. No mocks. AAA markers + one assert
per test (PA-307 STX-TQ002/TQ007). The audit is a pure function over
the filesystem; we exercise it against real ``Path`` trees and assert
on the structured result.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._workdir_audit import (
    WorkdirClaudeAudit,
    _measure_top_level,
    audit_workdir_claude,
    bloat_subdir_threshold_files,
    to_dict,
    warn_threshold_bytes,
    warn_threshold_files,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _populate_subdir(parent: Path, rel: str, file_count: int) -> None:
    """Create ``rel`` under ``parent`` with ``file_count`` 1-byte files."""
    target = parent / rel
    target.mkdir(parents=True, exist_ok=True)
    for i in range(file_count):
        (target / f"f{i}").write_bytes(b"x")


@pytest.fixture
def healthy_workdir(tmp_path: Path) -> Path:
    """Small `.claude/` tree well under every threshold."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "hooks", 10)
    _populate_subdir(claude, "skills", 5)
    return tmp_path


@pytest.fixture
def bloated_workdir(tmp_path: Path) -> Path:
    """`.claude/` tree mirroring the orochi failure mode:
    big `worktrees` AND big `hooks/pre-tool-use/.pending`.
    """
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "hooks", 10)
    _populate_subdir(claude, "skills", 5)
    _populate_subdir(claude, "worktrees", 1_500)
    _populate_subdir(claude, "hooks/pre-tool-use/.pending", 1_200)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(env_save_restore) -> None:
    """Each test starts with default thresholds."""
    env_save_restore.delete("SAC_WORKDIR_CLAUDE_WARN_BYTES")
    env_save_restore.delete("SAC_WORKDIR_CLAUDE_WARN_FILES")
    env_save_restore.delete("SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES")


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_warn_threshold_bytes_defaults_to_ten_mib() -> None:
    # Arrange
    expected = 10 * 1024 * 1024
    # Act
    actual = warn_threshold_bytes()
    # Assert
    assert actual == expected


def test_warn_threshold_files_defaults_to_five_thousand() -> None:
    # Arrange
    expected = 5_000
    # Act
    actual = warn_threshold_files()
    # Assert
    assert actual == expected


def test_bloat_subdir_threshold_files_defaults_to_one_thousand() -> None:
    # Arrange
    expected = 1_000
    # Act
    actual = bloat_subdir_threshold_files()
    # Assert
    assert actual == expected


@pytest.mark.parametrize(
    "env_name,getter,value",
    [
        ("SAC_WORKDIR_CLAUDE_WARN_BYTES", warn_threshold_bytes, 4242),
        ("SAC_WORKDIR_CLAUDE_WARN_FILES", warn_threshold_files, 314),
        (
            "SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES",
            bloat_subdir_threshold_files,
            27,
        ),
    ],
)
def test_threshold_env_override(env_save_restore, env_name, getter, value) -> None:
    # Arrange
    env_save_restore.set(env_name, str(value))
    # Act
    actual = getter()
    # Assert
    assert actual == value


@pytest.mark.parametrize(
    "env_name,getter,default",
    [
        ("SAC_WORKDIR_CLAUDE_WARN_BYTES", warn_threshold_bytes, 10 * 1024 * 1024),
        ("SAC_WORKDIR_CLAUDE_WARN_FILES", warn_threshold_files, 5_000),
        (
            "SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES",
            bloat_subdir_threshold_files,
            1_000,
        ),
    ],
)
def test_threshold_env_garbage_falls_back_to_default(
    env_save_restore, env_name, getter, default
) -> None:
    # Arrange — garbage value should NOT silently zero the threshold.
    env_save_restore.set(env_name, "not-a-number")
    # Act
    actual = getter()
    # Assert
    assert actual == default


@pytest.mark.parametrize(
    "env_name,getter,default",
    [
        ("SAC_WORKDIR_CLAUDE_WARN_BYTES", warn_threshold_bytes, 10 * 1024 * 1024),
        ("SAC_WORKDIR_CLAUDE_WARN_FILES", warn_threshold_files, 5_000),
    ],
)
def test_threshold_env_zero_falls_back_to_default(
    env_save_restore, env_name, getter, default
) -> None:
    # Arrange — zero would defeat the protection; treat as garbage.
    env_save_restore.set(env_name, "0")
    # Act
    actual = getter()
    # Assert
    assert actual == default


# ---------------------------------------------------------------------------
# audit_workdir_claude — happy paths
# ---------------------------------------------------------------------------


def test_audit_returns_dataclass(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert isinstance(result, WorkdirClaudeAudit)


def test_audit_counts_files_in_healthy_tree(healthy_workdir: Path) -> None:
    # Arrange — fixture has 10 + 5 = 15 files.
    expected = 15
    # Act
    result = audit_workdir_claude(healthy_workdir)
    # Assert
    assert result.files == expected


def test_audit_sums_bytes_in_healthy_tree(healthy_workdir: Path) -> None:
    """Bytes total is >= sum of file st_sizes. The gdu/du tier reports
    APPARENT size including directory entries (~4 KiB each on ext4),
    so the audit returns >= 15. The lead's 2026-06-04 directive
    explicitly accepts non-exact totals — the audit's contract is
    "is it heavy? yes/no", not exact bytes."""
    # Arrange — fixture has 15 × 1-byte files; floor on bytes is 15.
    # Act
    result = audit_workdir_claude(healthy_workdir)
    # Assert
    assert result.bytes >= 15


def test_audit_healthy_tree_does_not_exceed_files(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.exceeded_files is False


def test_audit_healthy_tree_does_not_exceed_bytes(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.exceeded_bytes is False


def test_audit_healthy_tree_has_no_bloat_sources(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.bloat_sources == ()


# ---------------------------------------------------------------------------
# audit_workdir_claude — bloat detection
# ---------------------------------------------------------------------------


def test_audit_bloated_tree_files_exceed_threshold(
    bloated_workdir: Path,
) -> None:
    # Arrange — fixture has 10 + 5 + 1500 + 1200 = 2715 files on disk,
    # but `worktrees/` is PRUNED from the top-level walk by the shared
    # exclusion (see _walk_exclusions); the audit's TOTAL excludes those
    # 1500. Effective total for thresholding: 10 + 5 + 1200 = 1215.
    # Use a threshold below 1215 to assert the threshold-exceeded path.
    # Bump the BYTE threshold beyond gdu's disk-usage report (each
    # 1-byte file allocates a 4 KiB block, so 1215 files ≈ 5 MiB of
    # disk usage; without bumping, the byte-side early-exit would
    # fire first and short-circuit the file-count check).
    import os

    os.environ["SAC_WORKDIR_CLAUDE_WARN_FILES"] = "1000"
    os.environ["SAC_WORKDIR_CLAUDE_WARN_BYTES"] = "1073741824"  # 1 GiB
    try:
        # Act
        result = audit_workdir_claude(bloated_workdir)
    finally:
        del os.environ["SAC_WORKDIR_CLAUDE_WARN_FILES"]
        del os.environ["SAC_WORKDIR_CLAUDE_WARN_BYTES"]
    # Assert
    assert result.exceeded_files is True


def test_audit_totals_exclude_worktrees_subtree(tmp_path: Path) -> None:
    """The 2026-06-04 F-CS8 fix: a bloated `<workdir>/.claude/worktrees/`
    does NOT count toward the audit's `files` total. Without the prune
    a worktree-heavy workdir would push totals into the warn band and
    add O(N×worktree-tree-size) cost to every agent boot."""
    # Arrange — ONLY worktrees has files (heavily bloated); everything
    # else under .claude/ is empty.
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "worktrees", 10_000)
    # Act
    result = audit_workdir_claude(tmp_path)
    # Assert — totals exclude worktrees entirely
    assert result.files == 0


def test_audit_bytes_total_excludes_worktrees_subtree(tmp_path: Path) -> None:
    """Companion to the files-total fix: a heavy worktrees/ subtree
    does NOT dominate the bytes total. The audit's bytes may be a few
    KiB of dir-entry overhead (when measured by du/gdu), but stays
    orders of magnitude below the worktrees payload — that's the
    F-CS8 fix in action."""
    # Arrange — 100 × 1 KiB files in worktrees = 100 KiB of worktree payload.
    claude = tmp_path / ".claude"
    claude.mkdir()
    wt = claude / "worktrees" / "agent-x"
    wt.mkdir(parents=True)
    for i in range(100):
        (wt / f"f{i}.bin").write_bytes(b"x" * 1024)
    # Act
    result = audit_workdir_claude(tmp_path)
    # Assert — must be << 100 KiB (without the fix, it'd be >= 100 KiB)
    assert result.bytes < 10 * 1024


def test_audit_probe_still_reports_worktrees_after_prune_fix(
    tmp_path: Path,
) -> None:
    """The per-subdir probe is BASENAME-keyed exclusion-aware: the
    `worktrees/` prune applies when WALKING THROUGH the basename,
    but the probe is rooted INSIDE `worktrees/` so its entries
    (``agent-*``) are NOT excluded. Per-bucket telemetry preserved."""
    # Arrange — single-bucket bloat in worktrees only
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "worktrees", 1_500)
    # Act
    result = audit_workdir_claude(tmp_path)
    # Assert
    assert any(
        s.rel_path == "worktrees" and s.files == 1_500 for s in result.bloat_sources
    )


def test_audit_bloated_tree_lists_worktrees_as_bloat_source(
    bloated_workdir: Path,
) -> None:
    # Arrange — worktrees subdir has 1500 > 1000 default subdir threshold.
    # Act
    result = audit_workdir_claude(bloated_workdir)
    # Assert
    assert any(s.rel_path == "worktrees" for s in result.bloat_sources)


def test_audit_bloated_tree_lists_pending_as_bloat_source(
    bloated_workdir: Path,
) -> None:
    # Arrange — pending subdir has 1200 > 1000 default subdir threshold.
    # Act
    result = audit_workdir_claude(bloated_workdir)
    # Assert
    assert any(
        s.rel_path == "hooks/pre-tool-use/.pending" for s in result.bloat_sources
    )


def test_audit_bloat_sources_sorted_desc_by_files(bloated_workdir: Path) -> None:
    # Arrange — worktrees has 1500, pending has 1200. Worst-first ordering.
    # Act
    result = audit_workdir_claude(bloated_workdir)
    files_counts = [s.files for s in result.bloat_sources]
    # Assert
    assert files_counts == sorted(files_counts, reverse=True)


def test_audit_subdir_under_bloat_threshold_not_listed(tmp_path: Path) -> None:
    # Arrange — populate worktrees with fewer files than the default 1000
    # bloat-subdir threshold; it should NOT show up in bloat_sources.
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "worktrees", 50)
    # Act
    result = audit_workdir_claude(tmp_path)
    # Assert
    assert result.bloat_sources == ()


def test_audit_custom_probed_subdirs_only_reports_listed(
    bloated_workdir: Path,
) -> None:
    # Arrange — restrict probing to just worktrees; pending must NOT
    # appear even though it is over the per-subdir threshold.
    probed = ("worktrees",)
    # Act
    result = audit_workdir_claude(bloated_workdir, probed_subdirs=probed)
    rel_paths = {s.rel_path for s in result.bloat_sources}
    # Assert
    assert rel_paths == {"worktrees"}


# ---------------------------------------------------------------------------
# _measure_top_level — bounded walk with EARLY-EXIT at threshold
# ---------------------------------------------------------------------------


def test_measure_top_level_early_exits_when_file_threshold_crossed(
    tmp_path: Path,
) -> None:
    """The threshold-bounded walk must STOP as soon as file count
    crosses the threshold, returning ``early_exit=True``. Cost stays
    at O(threshold), not O(tree)."""
    # Arrange — 1000 files, threshold 100; expect early-exit
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "skills", 1_000)
    # Act
    files, _bytes, early_exit = _measure_top_level(
        claude, file_threshold=100, byte_threshold=10**12
    )
    # Assert
    assert early_exit is True and files > 100


def test_measure_top_level_early_exits_when_byte_threshold_crossed(
    tmp_path: Path,
) -> None:
    """Byte-side early exit: a few bytes over threshold triggers
    early_exit even though file count is tiny."""
    # Arrange — single 1 KiB file; byte threshold 100
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "big.bin").write_bytes(b"x" * 1024)
    # Act
    _files, byte_count, early_exit = _measure_top_level(
        claude, file_threshold=10**9, byte_threshold=100
    )
    # Assert
    assert early_exit is True and byte_count > 100


def test_measure_top_level_completes_when_under_thresholds(tmp_path: Path) -> None:
    """When the tree is well under both thresholds the walk completes
    and returns exact totals + early_exit=False."""
    # Arrange — 5 files, well under any reasonable threshold
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "skills", 5)
    # Act
    files, _bytes, early_exit = _measure_top_level(
        claude, file_threshold=1_000_000, byte_threshold=10**12
    )
    # Assert
    assert early_exit is False and files == 5


def test_measure_top_level_excludes_worktrees_from_totals(tmp_path: Path) -> None:
    """The shared prune predicate fires inside _measure_top_level —
    worktrees/ subtrees do not contribute to files or bytes."""
    # Arrange — 1000 files in worktrees, 5 in skills; threshold higher
    # than skills count so the walk completes (no early exit).
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "worktrees", 1_000)
    _populate_subdir(claude, "skills", 5)
    # Act
    files, _bytes, _early_exit = _measure_top_level(
        claude, file_threshold=1_000_000, byte_threshold=10**12
    )
    # Assert
    assert files == 5


def test_audit_uses_threshold_bounded_measurement(tmp_path: Path) -> None:
    """End-to-end: an oversized .claude/ (well past threshold, no
    worktrees involved) returns ``exceeded_files=True`` without
    counting every file — the early-exit path. Bump the byte
    threshold beyond gdu's disk-usage report so the file-side path
    triggers first (gdu reports block-aligned bytes, so 8000 ×
    1-byte files ≈ 32 MiB of disk usage; without the bump the
    byte-side early-exit would fire and short-circuit the file
    check before the threshold-bounded loop completes a thousand
    iterations)."""
    import os

    # Arrange — 8000 1-byte files; default file threshold 5000.
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "skills", 8_000)
    os.environ["SAC_WORKDIR_CLAUDE_WARN_BYTES"] = "1073741824"  # 1 GiB
    try:
        # Act
        result = audit_workdir_claude(tmp_path)
    finally:
        del os.environ["SAC_WORKDIR_CLAUDE_WARN_BYTES"]
    # Assert
    assert result.exceeded_files is True


# ---------------------------------------------------------------------------
# audit_workdir_claude — missing / empty inputs
# ---------------------------------------------------------------------------


def test_audit_none_workdir_returns_missing(_clean_env=None) -> None:
    # Arrange
    workdir: None = None
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.missing is True


def test_audit_empty_workdir_returns_missing(_clean_env=None) -> None:
    # Arrange
    workdir = ""
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.missing is True


def test_audit_workdir_without_claude_subdir_returns_missing(
    tmp_path: Path,
) -> None:
    # Arrange — tmp_path has no .claude/ child.
    workdir = tmp_path
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.missing is True


def test_audit_missing_tree_zero_files(tmp_path: Path) -> None:
    # Arrange
    workdir = tmp_path
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.files == 0


def test_audit_missing_tree_does_not_exceed_files(tmp_path: Path) -> None:
    # Arrange
    workdir = tmp_path
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.exceeded_files is False


# ---------------------------------------------------------------------------
# audit_workdir_claude — symlinks not followed
# ---------------------------------------------------------------------------


def test_audit_does_not_follow_symlinks(tmp_path: Path) -> None:
    # Arrange — link `.claude/skills` → an outside tree of 100 files.
    outside = tmp_path / "outside"
    outside.mkdir()
    for i in range(100):
        (outside / f"f{i}").write_bytes(b"x")
    claude = tmp_path / "workdir" / ".claude"
    claude.mkdir(parents=True)
    (claude / "skills").symlink_to(outside, target_is_directory=True)
    # Act
    result = audit_workdir_claude(tmp_path / "workdir")
    # Assert — the 100 outside files MUST NOT contribute to the count.
    assert result.files == 0


# ---------------------------------------------------------------------------
# Visible-fallback warnings — ywatanabe core rule: NO silent fallbacks
# ---------------------------------------------------------------------------


# No-monkeypatch policy (STX-NM002): the visible-fallback warnings are
# tested by spawning the audit in a REAL child Python process whose
# ``PATH`` env is constrained to a tmp dir containing only the binaries
# we choose. The child's ``shutil.which`` genuinely cannot find what's
# not there; the production code is invoked unmodified. "Present but
# failing" is exercised by dropping a real ``#!/bin/sh\nexit 1`` script
# at the expected name — also a real binary, not a mock.


_AUDIT_WARN_DRIVER = """\
import logging, sys
logging.basicConfig(level=logging.WARNING, format='%(message)s', stream=sys.stderr)
from scitex_agent_container._workdir_audit import audit_workdir_claude
audit_workdir_claude(sys.argv[1])
"""


_AUDIT_FILES_DRIVER = """\
import json, sys
from scitex_agent_container._workdir_audit import audit_workdir_claude, to_dict
print(json.dumps(to_dict(audit_workdir_claude(sys.argv[1]))))
"""


# The child process needs the in-worktree src/ on PYTHONPATH so the
# constrained-PATH child still imports OUR scitex_agent_container, not
# whatever's installed in the system venv.
_TESTS_SRC_ROOT = str((Path(__file__).resolve().parents[2] / "src"))


def _write_real_shim(path: Path, body: str) -> None:
    """Drop a REAL executable shell script at ``path``. Not a mock —
    a genuine binary that subprocess actually executes."""
    path.write_text(body)
    path.chmod(0o755)


def _run_in_child(
    driver: str, workdir: Path, path_dir: Path
) -> subprocess.CompletedProcess:
    """Run ``driver`` in a child Python process with PATH constrained to
    ``path_dir`` and the workdir passed as argv[1]. Returns the
    CompletedProcess so the caller can inspect stdout and stderr."""
    import sys as _sys

    env = {
        "PATH": str(path_dir),
        "PYTHONPATH": _TESTS_SRC_ROOT,
        "HOME": str(workdir),
    }
    return subprocess.run(
        [_sys.executable, "-c", driver, str(workdir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_audit_warns_when_gdu_missing_from_path(tmp_path: Path) -> None:
    """No-silent-fallback discipline: gdu absent from a real-empty PATH
    must emit a WARNING line naming gdu. Real subprocess + real
    constrained PATH — no monkeypatch."""
    # Arrange
    bin_dir = tmp_path / "bin_empty"
    bin_dir.mkdir()
    claude = tmp_path / "wd" / ".claude"
    claude.mkdir(parents=True)
    _populate_subdir(claude, "skills", 3)
    # Act
    proc = _run_in_child(_AUDIT_WARN_DRIVER, tmp_path / "wd", bin_dir)
    # Assert
    assert "gdu not found" in proc.stderr


def test_audit_warns_when_du_missing_from_path(tmp_path: Path) -> None:
    """Second-tier discipline: when both gdu and du are absent the
    fallback to bounded os.walk must fire its own WARNING."""
    # Arrange
    bin_dir = tmp_path / "bin_empty"
    bin_dir.mkdir()
    claude = tmp_path / "wd" / ".claude"
    claude.mkdir(parents=True)
    _populate_subdir(claude, "skills", 3)
    # Act
    proc = _run_in_child(_AUDIT_WARN_DRIVER, tmp_path / "wd", bin_dir)
    # Assert
    assert "du not found" in proc.stderr


def test_audit_warns_when_gdu_present_but_exits_nonzero(tmp_path: Path) -> None:
    """gdu on PATH but the binary returns non-zero → chain falls
    through to du with a visible WARNING. Uses a REAL `exit 1` shell
    script as the gdu binary."""
    # Arrange — real failing-gdu shim; real working-du shim so this
    # test targets specifically the gdu-failed branch.
    bin_dir = tmp_path / "bin_failing"
    bin_dir.mkdir()
    _write_real_shim(bin_dir / "gdu", "#!/bin/sh\nexit 1\n")
    real_du = shutil.which("du")
    if real_du is None:
        pytest.skip("du not present on host — cannot scope the failing-gdu test")
    _write_real_shim(bin_dir / "du", f'#!/bin/sh\nexec "{real_du}" "$@"\n')
    claude = tmp_path / "wd" / ".claude"
    claude.mkdir(parents=True)
    _populate_subdir(claude, "skills", 3)
    # Act
    proc = _run_in_child(_AUDIT_WARN_DRIVER, tmp_path / "wd", bin_dir)
    # Assert
    assert "gdu invocation failed" in proc.stderr


def test_audit_still_returns_valid_result_with_empty_path(
    tmp_path: Path,
) -> None:
    """The bounded Python walk runs even with no external tools — the
    audit succeeds and the file count is correct. Uses subprocess +
    real empty PATH so neither gdu nor du resolves."""
    # Arrange
    bin_dir = tmp_path / "bin_empty"
    bin_dir.mkdir()
    claude = tmp_path / "wd" / ".claude"
    claude.mkdir(parents=True)
    _populate_subdir(claude, "skills", 7)
    # Act
    proc = _run_in_child(_AUDIT_FILES_DRIVER, tmp_path / "wd", bin_dir)
    import json as _json

    audit = _json.loads(proc.stdout)
    # Assert
    assert audit["files"] == 7


# ---------------------------------------------------------------------------
# gdu JSON parser — pure-function unit tests (no subprocess)
# ---------------------------------------------------------------------------


def test_gdu_json_parser_sums_asize_recursively():
    """Recursive sum across a synthetic gdu JSON tree mirroring the
    real schema: outer wrapper, root folder list, files with asize."""
    from scitex_agent_container._workdir_audit import _sum_asize_from_gdu_json

    # Arrange — directory at root has two child files (3 + 5 = 8 apparent).
    blob = (
        '[1,2,{"progname":"gdu","progver":"5.x","timestamp":0},'
        '[{"name":"/r","mtime":0},'
        '{"name":"a","asize":3,"dsize":4096,"mtime":0},'
        '{"name":"b","asize":5,"dsize":4096,"mtime":0}]]'
    )
    # Act
    total = _sum_asize_from_gdu_json(blob)
    # Assert
    assert total == 8


def test_gdu_json_parser_descends_into_subfolders():
    """A sub-folder is itself a list of [folder-meta, ...children];
    the recursion must descend into it and sum nested files."""
    from scitex_agent_container._workdir_audit import _sum_asize_from_gdu_json

    blob = (
        '[1,2,{"progname":"gdu"},'
        '[{"name":"/r"},'
        '{"name":"top","asize":10,"dsize":4096},'
        '[{"name":"sub"},'
        '{"name":"nested","asize":20,"dsize":4096}]]]'
    )
    # Act
    total = _sum_asize_from_gdu_json(blob)
    # Assert — top(10) + nested(20) = 30
    assert total == 30


def test_gdu_json_parser_returns_none_on_invalid_json():
    """Malformed JSON returns None so the caller can degrade."""
    from scitex_agent_container._workdir_audit import _sum_asize_from_gdu_json

    # Arrange
    blob = "this is not json"
    # Act
    total = _sum_asize_from_gdu_json(blob)
    # Assert
    assert total is None


def test_gdu_json_parser_returns_none_on_unexpected_shape():
    """Wrong top-level shape (not a length-4+ list) returns None."""
    from scitex_agent_container._workdir_audit import _sum_asize_from_gdu_json

    # Arrange — looks like JSON but doesn't match gdu's schema.
    blob = '{"unexpected": "shape"}'
    # Act
    total = _sum_asize_from_gdu_json(blob)
    # Assert
    assert total is None


# ---------------------------------------------------------------------------
# to_dict projection (status JSON / external consumers)
# ---------------------------------------------------------------------------


def test_to_dict_round_trips_workdir(healthy_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(healthy_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["workdir"] == str(healthy_workdir)


def test_to_dict_round_trips_files(healthy_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(healthy_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["files"] == audit.files


def test_to_dict_bloat_sources_are_dicts(bloated_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(bloated_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert all(isinstance(s, dict) for s in d["bloat_sources"])


def test_to_dict_bloat_sources_have_rel_path(bloated_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(bloated_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert all("rel_path" in s for s in d["bloat_sources"])


def test_to_dict_carries_missing_flag(tmp_path: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(tmp_path)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["missing"] is True


def test_to_dict_carries_threshold_files() -> None:
    # Arrange
    audit = audit_workdir_claude(None)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["threshold_files"] == 5_000
