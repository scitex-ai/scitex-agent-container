"""Mirror tests for ``heavy_job_demotion_policy.py`` (policy data).

Drives the policy module directly via a file-path import (the
``_baseline_assets`` asset tree is not an importable package). Asserts
the data contract the engine and the educational messages rely on: the
taught demotion prefix (best-effort-low, NOT idle — the field-tested
2026-07-10 rationale), the class catalogue, the knob helpers, and the
block-message content.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_POLICY_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "heavy_job_hooks"
    / "heavy_job_demotion_policy.py"
)
_spec = importlib.util.spec_from_file_location(
    "heavy_job_demotion_policy", _POLICY_PATH
)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


def test_policy_module_file_exists():
    # Arrange
    path = _POLICY_PATH
    # Act
    present = path.is_file()
    # Assert
    assert present, f"missing policy module: {path}"


def test_demote_prefix_is_nice19_plus_best_effort_lowest_io():
    # Arrange — the empirically-correct prefix: ionice -c 3 (idle)
    # starved/killed a real mksquashfs stage under load on 2026-07-10;
    # -c 2 -n 7 completed fine.
    expected = "nice -n 19 ionice -c 2 -n 7"
    # Act
    prefix = policy.DEMOTE_PREFIX
    # Assert
    assert prefix == expected


def test_demote_prefix_never_uses_idle_io_class():
    # Arrange
    idle_class = "-c 3"
    # Act
    prefix = policy.DEMOTE_PREFIX
    # Assert
    assert idle_class not in prefix


def test_image_build_subcommands_cover_direct_apptainer_and_docker():
    # Arrange
    builders = policy.IMAGE_BUILD_SUBCOMMANDS
    # Act
    covered = ("build",) in builders["apptainer"] and ("build",) in builders[
        "docker"
    ]
    # Assert
    assert covered


def test_plain_gzip_is_deliberately_not_a_gated_compressor():
    # Arrange — single-file gzip is common, brief, low-impact (module
    # docstring); gating it would be pure friction.
    compressors = policy.COMPRESSORS
    # Act
    gated = "gzip" in compressors
    # Assert
    assert not gated


def test_every_edu_class_used_by_rules_has_text():
    # Arrange
    required = {
        "image_build", "squashfs", "compress", "archive",
        "parallel_build", "sac_no_nice", "extra", "default",
    }
    # Act
    missing = required - set(policy.EDU)
    # Assert
    assert not missing, f"EDU entries missing for: {missing}"


def test_jobs_max_defaults_to_four():
    # Arrange
    saved = os.environ.pop(policy.JOBS_MAX_ENV, None)
    # Act
    try:
        value = policy.jobs_max()
    finally:
        if saved is not None:
            os.environ[policy.JOBS_MAX_ENV] = saved
    # Assert
    assert value == 4


def test_jobs_max_honours_env_override():
    # Arrange
    saved = os.environ.get(policy.JOBS_MAX_ENV)
    os.environ[policy.JOBS_MAX_ENV] = "8"
    # Act
    try:
        value = policy.jobs_max()
    finally:
        if saved is None:
            os.environ.pop(policy.JOBS_MAX_ENV, None)
        else:
            os.environ[policy.JOBS_MAX_ENV] = saved
    # Assert
    assert value == 8


def test_jobs_max_falls_back_on_unparseable_env_value():
    # Arrange
    saved = os.environ.get(policy.JOBS_MAX_ENV)
    os.environ[policy.JOBS_MAX_ENV] = "not-a-number"
    # Act
    try:
        value = policy.jobs_max()
    finally:
        if saved is None:
            os.environ.pop(policy.JOBS_MAX_ENV, None)
        else:
            os.environ[policy.JOBS_MAX_ENV] = saved
    # Assert
    assert value == 4


def test_guard_disabled_treats_zero_as_active():
    # Arrange — same semantics as SAC_BUILD_NO_NICE: only empty/"0"
    # keep the guard on.
    saved = os.environ.get(policy.DISABLE_ENV)
    os.environ[policy.DISABLE_ENV] = "0"
    # Act
    try:
        disabled = policy.guard_disabled()
    finally:
        if saved is None:
            os.environ.pop(policy.DISABLE_ENV, None)
        else:
            os.environ[policy.DISABLE_ENV] = saved
    # Assert
    assert not disabled


def test_extend_deny_from_env_folds_comma_list_into_extra_deny():
    # Arrange
    saved = os.environ.get(policy.EXTRA_DENY_ENV)
    os.environ[policy.EXTRA_DENY_ENV] = "rsync, ffmpeg"
    policy.EXTRA_DENY.clear()
    # Act
    try:
        policy.extend_deny_from_env()
    finally:
        if saved is None:
            os.environ.pop(policy.EXTRA_DENY_ENV, None)
        else:
            os.environ[policy.EXTRA_DENY_ENV] = saved
    # Assert
    assert policy.EXTRA_DENY == {"rsync", "ffmpeg"}


def test_block_message_carries_the_corrected_prefix():
    # Arrange
    needle = policy.DEMOTE_PREFIX
    # Act
    message = policy.block_message("mksquashfs", "squashfs")
    # Assert
    assert needle in message


def test_block_message_explains_why_not_idle_io_class():
    # Arrange
    needle = "NOT idle (-c 3)"
    # Act
    message = policy.block_message("mksquashfs", "squashfs")
    # Assert
    assert needle in message


def test_block_message_advises_remote_first_route():
    # Arrange
    needle = "Spartan"
    # Act
    message = policy.block_message("xz", "compress")
    # Assert
    assert needle in message


def test_block_message_names_both_bypasses():
    # Arrange
    message = policy.block_message("pigz", "compress")
    # Act
    named = policy.ALLOW_ENV in message and policy.BYPASS_MARKER in message
    # Assert
    assert named


def test_block_message_unknown_class_falls_back_to_default_edu():
    # Arrange
    fallback_line = policy.EDU["default"]
    # Act
    message = policy.block_message("mystery", "not-a-class")
    # Assert
    assert fallback_line in message
