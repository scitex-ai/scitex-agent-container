"""Tests for ``_remote_bake_core.pull_and_publish`` — verify-then-swap.

WATCH-IT-FAIL coverage is the point of half these tests: a checksum
mismatch, a failed symbol probe and a dead rsync must each leave the live
symlinks UNTOUCHED — a guard only ever seen passing is a hope.

No mocks: the subprocess seam (``_remote_bake_core._run``) is swapped for
a hand-rolled REAL-behaviour fake (it writes real bytes to the rsync
destination and returns real ``CompletedProcess`` objects — same
save/restore pattern as ``test_image_group``), and every scenario runs
against a real ``tmp_path`` store shaped like the production layout.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg import _remote_bake_core as core
from scitex_agent_container.cli_pkg._remote_bake_core import (
    BakeVerdict,
    PullVerdict,
    RemoteBakeOutcome,
    pull_and_publish,
)

_OLD = "sac-base-2026-0710-000000.sif"
_NEW = "sac-base-2026-0717-000000.sif"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_store(tmp_path: Path, layer: str, names: list[str], live: str) -> Path:
    containers = tmp_path / "containers"
    layer_dir = containers / f"sac-{layer}"
    layer_dir.mkdir(parents=True)
    for name in names:
        (layer_dir / name).write_bytes(b"sif:" + name.encode())
    (layer_dir / f"sac-{layer}.sif").symlink_to(live)
    (containers / f"sac-{layer}.sif").symlink_to(f"sac-{layer}/{live}")
    return containers


def _outcome(layer: str, name: str, payload: bytes) -> RemoteBakeOutcome:
    return RemoteBakeOutcome(
        verdict=BakeVerdict.BAKED,
        layer=layer,
        sif=f"/remote/store/sac-{layer}/{name}",
        sha256=_sha256(payload),
    )


class _RecordingRunner:
    """Real-behaviour stand-in for ``subprocess.run``.

    The rsync leg writes real bytes to the destination path (or fails
    with rc=23 when ``rsync_payload is None``); the apptainer leg exits
    ``probe_rc``. Every argv is recorded for order/count assertions.
    """

    def __init__(self, *, rsync_payload: bytes | None, probe_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._rsync_payload = rsync_payload
        self._probe_rc = probe_rc

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        exe = Path(args[0]).name
        if exe == "rsync":
            if self._rsync_payload is None:
                return subprocess.CompletedProcess(
                    args, 23, "", "rsync: link_stat failed"
                )
            Path(args[-1]).write_bytes(self._rsync_payload)
            return subprocess.CompletedProcess(args, 0, "", "")
        if exe == "apptainer":
            out = "OK" if self._probe_rc == 0 else "FATAL: probe failed"
            return subprocess.CompletedProcess(args, self._probe_rc, out, "")
        raise AssertionError(f"unexpected subprocess: {args}")


@pytest.fixture()
def seam():
    """Save/restore the module-level seams around each test.

    ``_which`` is pinned to a fixed path so the TEST host's toolbox (a
    SIF without rsync, say) never decides the scenario outcome.
    """
    saved_run, saved_which = core._run, core._which
    core._which = lambda name: f"/usr/bin/{name}"

    def _swap(runner: _RecordingRunner) -> _RecordingRunner:
        core._run = runner
        return runner

    yield _swap
    core._run, core._which = saved_run, saved_which


def _pull(containers: Path, payload_promised: bytes) -> object:
    return pull_and_publish(
        host="spartan",
        outcome=_outcome("base", _NEW, payload_promised),
        containers_dir=containers,
        retain=3,
        apptainer="/usr/bin/apptainer",
    )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_verified_pull_reports_swapped(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"fresh"))
    # Act
    result = _pull(containers, b"fresh")
    # Assert
    assert result.verdict is PullVerdict.SWAPPED


def test_verified_pull_flips_the_top_level_symlink(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"fresh"))
    # Act
    _pull(containers, b"fresh")
    # Assert
    assert (containers / "sac-base.sif").resolve().name == _NEW


def test_verified_pull_flips_the_inner_boot_symlink(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"fresh"))
    # Act
    _pull(containers, b"fresh")
    # Assert
    assert (containers / "sac-base" / "sac-base.sif").resolve().name == _NEW


def test_verified_pull_writes_the_checksum_sidecar(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"fresh"))
    # Act
    _pull(containers, b"fresh")
    # Assert
    sidecar = containers / "sac-base" / (_NEW + ".sha256")
    assert sidecar.read_text().startswith(_sha256(b"fresh"))


def test_verified_pull_runs_rsync_then_probe_through_the_seam(
    tmp_path: Path, seam
) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    runner = seam(_RecordingRunner(rsync_payload=b"fresh"))
    # Act
    _pull(containers, b"fresh")
    # Assert
    assert [Path(c[0]).name for c in runner.calls] == ["rsync", "apptainer"]


# ---------------------------------------------------------------------------
# WATCH IT FAIL — each broken leg leaves the live image untouched
# ---------------------------------------------------------------------------


def test_checksum_mismatch_reports_failed(tmp_path: Path, seam) -> None:
    # Arrange — the transfer delivers DIFFERENT bytes than the remote
    # sidecar promised (transport rot).
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"corrupted"))
    # Act
    result = _pull(containers, b"the-real-bytes")
    # Assert
    assert result.verdict is PullVerdict.FAILED


def test_checksum_mismatch_names_both_checksums(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"corrupted"))
    # Act
    result = _pull(containers, b"the-real-bytes")
    # Assert
    assert "checksum mismatch" in result.detail


def test_checksum_mismatch_leaves_the_live_symlink_untouched(
    tmp_path: Path, seam
) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"corrupted"))
    # Act
    _pull(containers, b"the-real-bytes")
    # Assert
    assert (containers / "sac-base.sif").resolve().name == _OLD


def test_checksum_mismatch_never_lands_a_final_named_artifact(
    tmp_path: Path, seam
) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"corrupted"))
    # Act
    _pull(containers, b"the-real-bytes")
    # Assert
    assert not (containers / "sac-base" / _NEW).exists()


def test_checksum_mismatch_keeps_the_dot_partial_for_resume(
    tmp_path: Path, seam
) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"corrupted"))
    # Act
    _pull(containers, b"the-real-bytes")
    # Assert
    assert (containers / "sac-base" / f".incoming-{_NEW}").exists()


def test_probe_failure_reports_failed(tmp_path: Path, seam) -> None:
    # Arrange — checksum matches but the symbol probe REFUSES the artifact
    # (the 01:23Z shape: green build rc, wrong contents).
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"stale", probe_rc=1))
    # Act
    result = _pull(containers, b"stale")
    # Assert
    assert result.verdict is PullVerdict.FAILED


def test_probe_failure_names_the_gate(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"stale", probe_rc=1))
    # Act
    result = _pull(containers, b"stale")
    # Assert
    assert "symbol probe FAILED" in result.detail


def test_probe_failure_leaves_the_live_symlink_untouched(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=b"stale", probe_rc=1))
    # Act
    _pull(containers, b"stale")
    # Assert
    assert (containers / "sac-base.sif").resolve().name == _OLD


def test_rsync_failure_reports_failed(tmp_path: Path, seam) -> None:
    # Arrange — rsync dies (rc=23); rsync-partial must never read as done.
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=None))
    # Act
    result = _pull(containers, b"x")
    # Assert
    assert result.verdict is PullVerdict.FAILED


def test_rsync_failure_names_the_rc(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_OLD], live=_OLD)
    seam(_RecordingRunner(rsync_payload=None))
    # Act
    result = _pull(containers, b"x")
    # Assert
    assert "rsync rc=23" in result.detail


# ---------------------------------------------------------------------------
# idempotent daily re-run
# ---------------------------------------------------------------------------


def test_already_live_artifact_reports_up_to_date(tmp_path: Path, seam) -> None:
    # Arrange — the reported artifact is ALREADY live and checksum-matched
    # (the daily SKIPPED case).
    containers = _make_store(tmp_path, "base", [_NEW], live=_NEW)
    (containers / "sac-base" / _NEW).write_bytes(b"already-live")
    seam(_RecordingRunner(rsync_payload=b"never-used"))
    # Act
    result = _pull(containers, b"already-live")
    # Assert
    assert result.verdict is PullVerdict.UP_TO_DATE


def test_already_live_artifact_transfers_nothing(tmp_path: Path, seam) -> None:
    # Arrange
    containers = _make_store(tmp_path, "base", [_NEW], live=_NEW)
    (containers / "sac-base" / _NEW).write_bytes(b"already-live")
    runner = seam(_RecordingRunner(rsync_payload=b"never-used"))
    # Act
    _pull(containers, b"already-live")
    # Assert
    assert runner.calls == []


def test_live_artifact_with_differing_checksum_refuses_to_guess(
    tmp_path: Path, seam
) -> None:
    # Arrange — the live local file exists under the reported name but its
    # bytes differ from the remote sidecar: two claimants, no arbiter —
    # refuse loudly rather than overwrite either.
    containers = _make_store(tmp_path, "base", [_NEW], live=_NEW)
    seam(_RecordingRunner(rsync_payload=b"never-used"))
    # Act
    result = _pull(containers, b"different-remote-bytes")
    # Assert
    assert result.verdict is PullVerdict.FAILED
