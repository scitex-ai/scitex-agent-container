"""Mirror tests for ``heavy_job_demotion_core.py`` (parsing engine).

The behavioral suite (subprocess-driven, real PreToolUse payloads,
knobs, bypasses) lives in ``tests/integration/heavy_job_hooks/``. THIS
mirror file drives the engine's pure helpers directly — the wrapper
unwrapping (demotion detection), the jobs/tar/subcommand parsers, and
the pipeline judge's verdicts — via a file-path import (the
``_baseline_assets`` asset tree is not an importable package; the
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
    / "heavy_job_hooks"
    / "heavy_job_demotion_core.py"
)
_spec = importlib.util.spec_from_file_location(
    "heavy_job_demotion_core", _CORE_PATH
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
    cmd = "ls -la && mksquashfs a b"
    # Act
    segs = core._split_segments(cmd)
    # Assert
    assert segs == ["ls -la", "mksquashfs a b"]


def test_split_segments_swallows_heredoc_body():
    # Arrange
    cmd = "cat <<EOF > build.sh\nmksquashfs a b\nEOF\nls"
    # Act
    segs = core._split_segments(cmd)
    # Assert
    assert not any("mksquashfs" in seg for seg in segs), segs


def test_unwrap_reports_nice_in_wrapper_chain():
    # Arrange
    toks = ["nice", "-n", "19", "tar", "czf", "big.tgz", "data/"]
    # Act
    remaining, demoted = core._unwrap(toks)
    # Assert
    assert demoted and remaining[0] == "tar"


def test_unwrap_reports_ionice_in_wrapper_chain():
    # Arrange
    toks = ["ionice", "-c", "2", "-n", "7", "mksquashfs", "a", "b"]
    # Act
    remaining, demoted = core._unwrap(toks)
    # Assert
    assert demoted and remaining[0] == "mksquashfs"


def test_unwrap_without_demotion_wrapper_reports_undemoted():
    # Arrange
    toks = ["timeout", "60", "mksquashfs", "a", "b"]
    # Act
    remaining, demoted = core._unwrap(toks)
    # Assert
    assert not demoted and remaining[0] == "mksquashfs"


def test_parse_jobs_reads_attached_value():
    # Arrange
    rest = ["build", "-j12"]
    # Act
    jobs = core._parse_jobs(rest)
    # Assert
    assert jobs == 12


def test_parse_jobs_bare_flag_means_maximal():
    # Arrange
    rest = ["-j"]
    # Act
    jobs = core._parse_jobs(rest)
    # Assert
    assert jobs == -1


def test_parse_jobs_dynamic_value_means_maximal():
    # Arrange
    rest = ["-j$(nproc)"]
    # Act
    jobs = core._parse_jobs(rest)
    # Assert
    assert jobs == -1


def test_parse_jobs_absent_flag_returns_none():
    # Arrange
    rest = ["all"]
    # Act
    jobs = core._parse_jobs(rest)
    # Assert
    assert jobs is None


def test_tar_creates_detects_old_style_flag_cluster():
    # Arrange
    rest = ["czf", "out.tgz", "data/"]
    # Act
    creating = core._tar_creates(rest)
    # Assert
    assert creating


def test_tar_creates_ignores_extraction():
    # Arrange
    rest = ["-xf", "release.tar", "-C", "/tmp"]
    # Act
    creating = core._tar_creates(rest)
    # Assert
    assert not creating


def test_judge_pipeline_blocks_undemoted_mksquashfs():
    # Arrange
    cmd = "mksquashfs squashfs-root out.squashfs"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("mksquashfs", "squashfs")


def test_judge_pipeline_allows_fully_demoted_heavy_command():
    # Arrange
    cmd = "nice -n 19 ionice -c 2 -n 7 tar czf big.tgz data/"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_blocks_apptainer_build_subcommand():
    # Arrange
    cmd = "apptainer build out.sif recipe.def"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("apptainer build", "image_build")


def test_judge_pipeline_allows_apptainer_exec_subcommand():
    # Arrange
    cmd = "apptainer exec img.sif hostname"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_allows_plain_sac_image_build():
    # Arrange — `sac image build` self-demotes by default (PR #605).
    cmd = "sac image build base -y"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_blocks_sac_image_build_no_nice_flag():
    # Arrange
    cmd = "sac image build base -y --no-nice"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("sac image build --no-nice", "sac_no_nice")


def test_judge_pipeline_blocks_no_nice_env_assignment_prefix():
    # Arrange — SAC_BUILD_NO_NICE=1 disables sac's self-demotion, so the
    # assignment-prefixed form is the same incident shape as --no-nice.
    cmd = "SAC_BUILD_NO_NICE=1 sac image build base -y"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("sac image build --no-nice", "sac_no_nice")


def test_judge_pipeline_flags_heavy_second_segment():
    # Arrange
    cmd = "ls -la && xz -9 huge.log"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("xz", "compress")


def test_judge_pipeline_recurses_into_shell_dash_c():
    # Arrange
    cmd = "bash -c 'mksquashfs a b'"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("mksquashfs", "squashfs")


def test_judge_pipeline_demoted_shell_payload_inherits_priority():
    # Arrange — nice on the outer shell demotes every descendant.
    cmd = "nice -n 19 bash -c 'mksquashfs a b'"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_unwraps_xargs_into_compressor():
    # Arrange
    cmd = "fd -e log | xargs xz"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("xz", "compress")


def test_judge_pipeline_allows_make_at_or_below_jobs_threshold():
    # Arrange
    cmd = "make -j 4"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_blocks_make_above_jobs_threshold():
    # Arrange
    cmd = "make -j8"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict == ("make -j", "parallel_build")


def test_judge_pipeline_allows_compressor_version_introspection():
    # Arrange
    cmd = "xz --version"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None


def test_judge_pipeline_allows_opaque_script_invocation():
    # Arrange — a deny-list cannot judge a script file's contents.
    cmd = "bash run_build.sh"
    # Act
    verdict = core._judge_pipeline(cmd)
    # Assert
    assert verdict is None
