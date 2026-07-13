"""Tests for :mod:`scitex_agent_container._lifecycle._startup_failed`.

Real-FS marker writes against tmp_path. Pins:
  * the on-disk schema (schema_version, required keys),
  * the apptainer-FATAL pattern classifier (so wire callers see a stable
    ``kind`` set),
  * the tail-bytes bound (runaway stdout can't bloat the marker),
  * the atomic write (no partial marker visible to STATUS/DELETE readers).
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._lifecycle._startup_failed import (
    MARKER_FILENAME,
    SCHEMA_VERSION,
    classify_apptainer_failure,
    is_stillborn,
    read_marker,
    write_marker,
)

# ---------------------------------------------------------------------------
# classify_apptainer_failure — well-known FATAL shapes
# ---------------------------------------------------------------------------


def test_classify_apptainer_mount_failed_from_clew_capsule_trace() -> None:
    # Arrange — verbatim head of the clew-cohort-a-capsule-0201225 FATAL
    # captured during the 2026-06-02 triage. The classifier MUST pin this
    # to ``apptainer_mount_failed`` so clew launcher branches consistently.
    stderr = (
        "FATAL:   container creation failed: mount hook function failure: "
        "mount source /work/data/capsule-0201225 doesn't exist"
    )
    # Act
    kind, _ = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert
    assert kind == "apptainer_mount_failed"


def test_classify_apptainer_mount_failed_includes_remediation_hint() -> None:
    # Arrange
    stderr = "mount source /a doesn't exist"
    # Act
    _, hint = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert
    assert "bind sources" in hint


def test_classify_sif_invalid_for_corrupt_sif_message() -> None:
    # Arrange
    stderr = "FATAL: /home/me/sac-base.sif is not a valid SIF image"
    # Act
    kind, _ = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert
    assert kind == "sif_invalid"


def test_classify_disk_full_for_enospc() -> None:
    # Arrange
    stderr = "write error: No space left on device"
    # Act
    kind, _ = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert
    assert kind == "disk_full"


def test_classify_overlay_missing_from_stillborn_agent_trace() -> None:
    # Arrange — the verbatim FATAL a brand-new agent hit when its per-agent
    # overlay dir did not exist (2026-07-13). It used to fall through to
    # container_creation_unknown — a raw apptainer FATAL with no fix.
    stderr = (
        "FATAL:   while loading overlay images: failed to open overlay image "
        "/home/u/.scitex/agent-container/containers/overlays/new-agent/: "
        "failed to retrieve path for /.../overlays/new-agent/: lstat "
        "/.../overlays/new-agent: no such file or directory"
    )
    # Act
    kind, _ = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert
    assert kind == "overlay_missing"


def test_classify_overlay_missing_hint_names_the_mkdir_fix() -> None:
    # Arrange
    stderr = "FATAL:   while loading overlay images: failed to open overlay image /x/"
    # Act
    _, hint = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert — constitution: never ship a FATAL without an actionable hint.
    assert "mkdir -p" in hint


def test_classify_unknown_kind_for_unrecognised_blob() -> None:
    # Arrange
    stderr = "something we have never seen before, panic"
    # Act
    kind, _ = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert
    assert kind == "container_creation_unknown"


def test_classify_unknown_kind_emits_generic_remediation_hint() -> None:
    # Arrange
    stderr = "novel-failure-message"
    # Act
    _, hint = classify_apptainer_failure(stdout="", stderr=stderr)
    # Assert
    assert "unrecognised" in hint


def test_classify_concatenates_stdout_and_stderr() -> None:
    # Arrange — the trigger phrase lands in stdout, not stderr.
    stdout = "mount source /a doesn't exist"
    # Act
    kind, _ = classify_apptainer_failure(stdout=stdout, stderr="")
    # Assert
    assert kind == "apptainer_mount_failed"


# ---------------------------------------------------------------------------
# write_marker — file shape + atomicity
# ---------------------------------------------------------------------------


def test_write_marker_creates_runtime_dir_if_missing(tmp_path: Path) -> None:
    # Arrange — runtime_dir doesn't exist yet (early-FATAL case).
    runtime_dir = tmp_path / "stillborn" / "runtime"
    # Act
    write_marker(
        runtime_dir,
        started_at="2026-06-03T01:23:45Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="mount source /a doesn't exist",
    )
    # Assert
    assert (runtime_dir / MARKER_FILENAME).is_file()


def test_write_marker_returns_target_path(tmp_path: Path) -> None:
    # Arrange
    runtime_dir = tmp_path
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="mount source /a doesn't exist",
    )
    # Assert
    assert target == tmp_path / MARKER_FILENAME


def test_marker_payload_has_schema_version(tmp_path: Path) -> None:
    # Arrange
    runtime_dir = tmp_path
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="",
    )
    payload = json.loads(target.read_text())
    # Assert
    assert payload["schema_version"] == SCHEMA_VERSION


def test_marker_payload_carries_phase(tmp_path: Path) -> None:
    # Arrange
    runtime_dir = tmp_path
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="sdk_init",
        exit_code=1,
        stdout="",
        stderr="",
    )
    payload = json.loads(target.read_text())
    # Assert
    assert payload["phase"] == "sdk_init"


def test_marker_payload_carries_exit_code(tmp_path: Path) -> None:
    # Arrange
    runtime_dir = tmp_path
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=137,
        stdout="",
        stderr="",
    )
    payload = json.loads(target.read_text())
    # Assert
    assert payload["exit_code"] == 137


def test_marker_payload_carries_runtime_dir(tmp_path: Path) -> None:
    # Arrange — per clew review, the marker must echo its own host-
    # absolute runtime_dir so the DELETE 410 / STATUS bodies can
    # surface a ``see_also`` pointer without recomputing the path.
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="",
    )
    payload = json.loads(target.read_text())
    # Assert
    assert payload["runtime_dir"] == str(tmp_path.resolve())


def test_marker_payload_classifies_apptainer_fatal(tmp_path: Path) -> None:
    # Arrange
    runtime_dir = tmp_path
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="mount source /work/x doesn't exist",
    )
    payload = json.loads(target.read_text())
    # Assert
    assert payload["kind"] == "apptainer_mount_failed"


def test_marker_payload_uses_kind_override_when_given(tmp_path: Path) -> None:
    # Arrange — kind_override skips the auto-classifier.
    runtime_dir = tmp_path
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="to_home_deploy",
        exit_code=1,
        stdout="",
        stderr="mount source /a doesn't exist",  # would normally classify
        kind_override="to_home_deploy_failed",
    )
    payload = json.loads(target.read_text())
    # Assert
    assert payload["kind"] == "to_home_deploy_failed"


def test_marker_payload_truncates_runaway_stdout(tmp_path: Path) -> None:
    # Arrange — push stdout above the tail-byte bound; payload's
    # stdout_tail must be bounded so a runaway log can't bloat the
    # marker.
    runaway = "x" * 50_000  # 50 KB
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout=runaway,
        stderr="",
    )
    payload = json.loads(target.read_text())
    # Assert
    assert len(payload["stdout_tail"]) < len(runaway)


def test_marker_payload_truncated_tail_is_suffix(tmp_path: Path) -> None:
    # Arrange — captured tail must come from the END of the log (not the
    # start) so the FATAL line is preserved.
    log = "noise\n" * 5000 + "FATAL_MARKER_LINE_KEEP_ME"
    # Act
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr=log,
    )
    payload = json.loads(target.read_text())
    # Assert
    assert "FATAL_MARKER_LINE_KEEP_ME" in payload["stderr_tail"]


def test_write_marker_atomic_no_tmp_visible(tmp_path: Path) -> None:
    # Arrange — write must atomically replace; no .tmp file lingers.
    # Act
    write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="",
    )
    tmp_siblings = list(tmp_path.glob(f"{MARKER_FILENAME}.tmp"))
    # Assert
    assert tmp_siblings == []


def test_write_marker_payload_is_valid_json(tmp_path: Path) -> None:
    # Arrange — the marker must parse as JSON for read_marker.
    target = write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="",
    )
    # Act
    parsed = json.loads(target.read_text())
    # Assert
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# read_marker / is_stillborn
# ---------------------------------------------------------------------------


def test_read_marker_returns_none_when_absent(tmp_path: Path) -> None:
    # Arrange
    runtime_dir = tmp_path
    # Act
    result = read_marker(tmp_path)
    # Assert
    assert result is None


def test_read_marker_returns_dict_when_present(tmp_path: Path) -> None:
    # Arrange
    write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="",
    )
    # Act
    result = read_marker(tmp_path)
    # Assert
    assert isinstance(result, dict)


def test_read_marker_handles_corrupt_payload(tmp_path: Path) -> None:
    # Arrange — a hand-written non-JSON marker collapses to None.
    (tmp_path / MARKER_FILENAME).write_text("this is not json")
    # Act
    result = read_marker(tmp_path)
    # Assert
    assert result is None


def test_is_stillborn_false_when_no_marker(tmp_path: Path) -> None:
    # Arrange
    runtime_dir = tmp_path
    # Act
    flag = is_stillborn(tmp_path)
    # Assert
    assert flag is False


def test_is_stillborn_true_after_write(tmp_path: Path) -> None:
    # Arrange
    write_marker(
        tmp_path,
        started_at="2026-06-03T01:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr="",
    )
    # Act
    flag = is_stillborn(tmp_path)
    # Assert
    assert flag is True
