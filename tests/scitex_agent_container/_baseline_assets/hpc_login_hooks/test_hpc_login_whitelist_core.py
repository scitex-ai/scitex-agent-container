"""Mirror tests for ``hpc_login_whitelist_core.py`` (parsing engine).

The behavioral suite (subprocess-driven, real PreToolUse payloads,
hostname gate, bypasses) lives in ``tests/integration/hpc_login_hooks/``.
THIS mirror file drives the engine's pure parsing helpers directly —
the segment splitter's contract (separators, quotes, heredoc bodies,
redirections) and the pipeline judge's verdicts — via a file-path import
(the ``_baseline_assets`` asset tree is not an importable package; the
scripts deploy to ``$HOME/.claude/hooks/pre-tool-use/``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_CORE_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "hpc_login_hooks"
    / "hpc_login_whitelist_core.py"
)
_spec = importlib.util.spec_from_file_location(
    "hpc_login_whitelist_core", _CORE_PATH
)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


def test_core_module_file_exists():
    # Arrange
    path = _CORE_PATH
    # Act
    present = path.is_file()
    # Assert
    assert present, f"missing core module: {path}"


def test_split_segments_on_and_chain():
    # Arrange
    cmd = "ls -la && make -j4"
    # Act
    segs = core._split_segments(cmd)
    # Assert
    assert segs == ["ls -la", "make -j4"]


def test_split_segments_keeps_quoted_separators_intact():
    # Arrange
    cmd = "rg -N 'a && b | c' src/"
    # Act
    segs = core._split_segments(cmd)
    # Assert
    assert segs == ["rg -N 'a && b | c' src/"]


def test_split_segments_swallows_heredoc_body():
    # Arrange
    cmd = "cat <<EOF > job.sh\npytest tests/\nEOF\nsbatch job.sh"
    # Act
    segs = core._split_segments(cmd)
    # Assert
    assert not any("pytest" in seg for seg in segs), segs


def test_split_segments_treats_stderr_redirect_as_one_segment():
    # Arrange
    cmd = "ls missing 2>&1"
    # Act
    segs = core._split_segments(cmd)
    # Assert
    assert segs == ["ls missing 2>&1"]


def test_judge_pipeline_allows_whitelisted_pipeline():
    # Arrange
    cmd = "squeue --me | grep RUNNING"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_flags_heavy_second_segment():
    # Arrange
    cmd = "ls -la && make -j4"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("make", "build_test")


def test_judge_pipeline_recurses_into_shell_dash_c():
    # Arrange
    cmd = "bash -lc 'pip install torch'"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("pip", "pkg_env")


def test_judge_pipeline_unwraps_light_wrappers():
    # Arrange
    cmd = "timeout 7 git -C /repo status"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_gates_heavy_git_subcommand():
    # Arrange
    cmd = "git -C /repo gc --aggressive"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("git gc", "git_heavy")


def test_judge_pipeline_lets_slurm_dispatch_carry_any_payload():
    # Arrange
    cmd = "srun --overlap --jobid 12345 pytest tests/"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_resolve_hostname_honours_test_seam_failure_token():
    # Arrange
    import os

    os.environ["SAC_HPC_LOGIN_TEST_HOSTNAME"] = "__fail__"
    # Act
    try:
        resolved = core._resolve_hostname()
    finally:
        del os.environ["SAC_HPC_LOGIN_TEST_HOSTNAME"]
    # Assert
    assert resolved is None
