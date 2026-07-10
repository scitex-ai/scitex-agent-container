"""Regression tests for ``enforce_heavy_job_demotion.sh``.

P1 incident 2026-07-10 (incident-local-heavy-build): a full SIF rebake
ran at NORMAL priority on the operator's loaded interactive host — load
spiked past 50. This hook blocks known-HEAVY commands launched without
``nice``/``ionice`` and teaches the corrected command
(``nice -n 19 ionice -c 2 -n 7 <cmd>``), the remote-first route, and
the bypasses. These tests drive the real shell hook + python core via
subprocess with real PreToolUse JSON payloads — no mocks — and assert
the allow/block decision, the educational message content, the knobs,
and the fail-open paths.
"""

from __future__ import annotations

import subprocess

import pytest

from .conftest import (
    CORE_SCRIPT,
    HOOK_SCRIPT,
    POLICY_SCRIPT,
    run_hook,
    run_hook_raw,
)

# --- result fixtures (run the hook once; each test asserts one thing) --


@pytest.fixture(scope="module")
def self_test_result():
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT), "--self-test"], capture_output=True, text=True
    )


@pytest.fixture
def mksquashfs_block_result():
    return run_hook("mksquashfs squashfs-root out.squashfs")


@pytest.fixture
def apptainer_build_block_result():
    return run_hook("apptainer build out.sif recipe.def")


# --- asset presence ----------------------------------------------------


def test_hook_script_file_exists():
    # Arrange
    script = HOOK_SCRIPT
    # Act
    present = script.is_file()
    # Assert
    assert present, f"missing hook: {script}"


def test_hook_script_is_executable():
    # Arrange
    script = HOOK_SCRIPT
    # Act
    mode = script.stat().st_mode
    # Assert
    assert mode & 0o111, "hook is not executable"


def test_core_script_file_exists():
    # Arrange
    script = CORE_SCRIPT
    # Act
    present = script.is_file()
    # Assert
    assert present, f"missing core: {script}"


def test_policy_script_file_exists():
    # Arrange
    script = POLICY_SCRIPT
    # Act
    present = script.is_file()
    # Assert
    assert present, f"missing policy: {script}"


# --- the script's own self-test (broadest built-in coverage) ------------


def test_self_test_exits_zero(self_test_result):
    # Arrange
    res = self_test_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 0, res.stdout + res.stderr


def test_self_test_reports_no_failures(self_test_result):
    # Arrange
    res = self_test_result
    # Act
    out = res.stdout
    # Assert
    assert "fail=0" in out


# --- block: undemoted heavy commands ------------------------------------


def test_undemoted_mksquashfs_is_blocked(mksquashfs_block_result):
    # Arrange
    res = mksquashfs_block_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 2, res.stderr


def test_undemoted_apptainer_build_is_blocked(apptainer_build_block_result):
    # Arrange
    res = apptainer_build_block_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 2, res.stderr


def test_undemoted_tar_create_is_blocked():
    # Arrange
    cmd = "tar czf big.tgz data/"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 2, res.stderr


def test_high_parallelism_make_is_blocked():
    # Arrange
    cmd = "make -j16"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 2, res.stderr


def test_sac_image_build_no_nice_is_blocked():
    # Arrange — the --no-nice opt-out on an interactive host re-creates
    # the incident shape; plain `sac image build` self-demotes.
    cmd = "sac image build base -y --no-nice"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 2, res.stderr


# --- allow: demoted / light / self-demoting invocations ------------------


def test_fully_demoted_heavy_command_is_allowed():
    # Arrange
    cmd = "nice -n 19 ionice -c 2 -n 7 mksquashfs squashfs-root out.squashfs"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr


def test_plain_sac_image_build_is_allowed():
    # Arrange — self-demotes by default since PR #605.
    cmd = "sac image build base -y"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr


def test_tar_extraction_is_allowed():
    # Arrange
    cmd = "tar xzf release.tgz"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr


def test_low_parallelism_make_is_allowed():
    # Arrange
    cmd = "make -j2"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr


def test_everyday_light_command_is_allowed():
    # Arrange
    cmd = "git -C /repo status"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr


# --- educational message content -----------------------------------------


def test_block_message_teaches_corrected_prefix(mksquashfs_block_result):
    # Arrange
    res = mksquashfs_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "nice -n 19 ionice -c 2 -n 7" in stderr


def test_block_message_explains_not_idle_class(mksquashfs_block_result):
    # Arrange — the field-tested rationale: idle-class IO starved and
    # killed a real mksquashfs stage; best-effort-low is the fix.
    res = mksquashfs_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "NOT idle (-c 3)" in stderr


def test_block_message_advises_remote_first(mksquashfs_block_result):
    # Arrange
    res = mksquashfs_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "Spartan" in stderr


def test_block_message_points_at_self_demoting_sac_build(
    apptainer_build_block_result,
):
    # Arrange
    res = apptainer_build_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "sac image build" in stderr


# --- knobs + bypasses ------------------------------------------------------


def test_env_bypass_allows_heavy_command():
    # Arrange
    cmd = "mksquashfs a b"
    # Act
    res = run_hook(cmd, extra_env={"SAC_HEAVY_JOB_ALLOW": "1"})
    # Assert
    assert res.returncode == 0, res.stderr


def test_inline_marker_bypass_allows_heavy_command():
    # Arrange
    cmd = "mksquashfs a b # hook-bypass: heavy-job"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr


def test_dedicated_host_disable_knob_allows_heavy_command():
    # Arrange
    cmd = "mksquashfs a b"
    # Act
    res = run_hook(cmd, extra_env={"SAC_HEAVY_JOB_GUARD_DISABLE": "1"})
    # Assert
    assert res.returncode == 0, res.stderr


def test_jobs_threshold_knob_raises_allowed_parallelism():
    # Arrange
    cmd = "make -j8"
    # Act
    res = run_hook(cmd, extra_env={"SAC_HEAVY_JOB_JOBS_MAX": "8"})
    # Assert
    assert res.returncode == 0, res.stderr


def test_extra_deny_knob_extends_the_deny_set():
    # Arrange
    cmd = "rsync -a big-tree/ dest:/data/"
    # Act
    res = run_hook(cmd, extra_env={"SAC_HEAVY_JOB_EXTRA_DENY": "rsync"})
    # Assert
    assert res.returncode == 2, res.stderr


# --- fail-open safety -------------------------------------------------------


def test_non_bash_tool_payload_passes_through():
    # Arrange
    raw = '{"tool_name":"Edit","tool_input":{"file_path":"/tmp/x"}}'
    # Act
    res = run_hook_raw(raw)
    # Assert
    assert res.returncode == 0, res.stderr


def test_malformed_json_fails_open():
    # Arrange — mentions a heavy keyword so the fast-path does not
    # short-circuit before the core's JSON parse.
    raw = "this is not json but mentions mksquashfs"
    # Act
    res = run_hook_raw(raw)
    # Assert
    assert res.returncode == 0, res.stderr


def test_empty_command_passes_through():
    # Arrange
    cmd = ""
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr
