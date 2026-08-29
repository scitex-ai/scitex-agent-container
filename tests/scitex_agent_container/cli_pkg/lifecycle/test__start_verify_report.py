"""``_start_verify_report.report_start_result`` — the caller-visible verdict.

Pins the operator contract from the 2026-08-14 outage (Errno 98 / venv
refusal / stdio frame kill all died server-side behind a SUCC):

* a verified failure exits non-zero, carries the boot-log tail (the real
  error text) to the terminal, and names the file it was read from;
* an in-window non-answer is its own status (``start-unverified``), also
  non-zero, never collapsed into "failed" or "started";
* only a verified (or by-design unverifiable) launch keeps exit 0, and
  the legacy ``--json`` statuses (``dry_run_ok`` / ``already_running`` /
  ``started``) are preserved for scripts.

NO MOCKS — real emit closures, real ``LaunchVerdict`` values produced by
real ``verify_fn`` callables (the documented injection seam), a real
minimal config object. Each test: AAA markers, one assertion, 3+-word
name.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle._launch_verify import (
    UNVERIFIED,
    VERIFIED_FAILED,
    VERIFIED_UP,
    LaunchVerdict,
)
from scitex_agent_container.cli_pkg.lifecycle._start_verify_report import (
    report_start_result,
)

# ---------------------------------------------------------------------------
# Real collaborators.
# ---------------------------------------------------------------------------


def _config(name: str = "verify-x") -> SimpleNamespace:
    """Minimal real config carrying exactly the fields the report reads."""
    return SimpleNamespace(
        name=name,
        a2a=SimpleNamespace(port=None),
        claude=SimpleNamespace(auto_accept=True, flags=[]),
    )


_ERRNO98_TAIL = "OSError: [Errno 98] error while attempting to bind on address"


def _failed_verdict(**overrides) -> LaunchVerdict:
    fields = dict(
        status=VERIFIED_FAILED,
        evidence="container process is DEAD 3.2s after launch "
        "(runtime.is_running -> False) and no fresh heartbeat was ever written",
        log_path="/state/verify-x/stdout.log",
        log_tail=_ERRNO98_TAIL,
        waited_s=3.2,
        heartbeat_path="/state/verify-x/heartbeat.json",
    )
    fields.update(overrides)
    return LaunchVerdict(**fields)


def _up_verdict() -> LaunchVerdict:
    return LaunchVerdict(
        VERIFIED_UP,
        "ready: first fresh heartbeat at 2026-08-14T21:00:00+09:00 "
        "(state=starting, pid 4242), 4.0s after launch",
        None,
        "",
        4.0,
        "/state/verify-x/heartbeat.json",
    )


def _unverified_verdict() -> LaunchVerdict:
    return LaunchVerdict(
        UNVERIFIED,
        "no fresh heartbeat within 90s — the container process still "
        "reports running, but nothing proves the agent came up",
        "/state/verify-x/stdout.log",
        "",
        90.0,
        "/state/verify-x/heartbeat.json",
    )


def _verify_fn_returning(verdict: LaunchVerdict):
    """A REAL verify_fn (the documented seam) that yields ``verdict``."""

    def _verify(config, **kwargs):  # noqa: ARG001 - verify_launch keyword shape
        return verdict

    return _verify


def _report(verdict: LaunchVerdict, *, as_json: bool, emitted: list) -> bool:
    return report_start_result(
        _config(),
        noop=False,
        dry_run=False,
        foreground=False,
        one_shot=False,
        as_json=as_json,
        emit=emitted.append,
        launched_at=time.time(),
        host="test-host",
        host_workdir="/work",
        container_workdir="/work",
        location="test-host@/work:/work",
        verify_fn=_verify_fn_returning(verdict),
    )


@pytest.fixture
def not_in_sif() -> Iterator[None]:
    """Ensure the in-SIF skip cannot mask the verdict under test."""
    keys = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


# ---------------------------------------------------------------------------
# verified failure — exit non-zero, error text on the terminal.
# ---------------------------------------------------------------------------


def test_verified_failure_returns_false(not_in_sif) -> None:
    """A launch whose process died must flip the caller's exit code."""
    # Arrange
    emitted: list = []
    # Act
    ok = _report(_failed_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert ok is False


def test_verified_failure_json_status_is_start_failed(not_in_sif) -> None:
    """``--json`` says ``start-failed`` — never a false ``started``."""
    # Arrange
    emitted: list = []
    # Act
    _report(_failed_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert emitted[0]["status"] == "start-failed"


def test_verified_failure_json_carries_boot_log_tail(not_in_sif) -> None:
    """The real error text (the Errno 98 line) reaches the JSON caller."""
    # Arrange
    emitted: list = []
    # Act
    _report(_failed_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert "[Errno 98]" in emitted[0]["verify"]["boot_log_tail"]


def test_verified_failure_json_names_the_boot_log(not_in_sif) -> None:
    """The file the tail came from is named so the operator can go deeper."""
    # Arrange
    emitted: list = []
    # Act
    _report(_failed_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert emitted[0]["verify"]["boot_log"] == "/state/verify-x/stdout.log"


def test_verified_failure_prints_error_text_to_terminal(
    not_in_sif, capsys
) -> None:
    """Human mode: the Errno 98 line must reach the caller's terminal —
    the operator's verbatim requirement (エラーが握りつぶされないこと)."""
    # Arrange
    emitted: list = []
    # Act
    _report(_failed_verdict(), as_json=False, emitted=emitted)
    # Assert
    captured = capsys.readouterr()
    assert "[Errno 98]" in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# unverified — the third value, distinct wording, still non-zero.
# ---------------------------------------------------------------------------


def test_unverified_launch_returns_false(not_in_sif) -> None:
    """"Could not verify" is exit-nonzero like a failure."""
    # Arrange
    emitted: list = []
    # Act
    ok = _report(_unverified_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert ok is False


def test_unverified_json_status_is_start_unverified(not_in_sif) -> None:
    """The third value keeps its own status — no binary collapse."""
    # Arrange
    emitted: list = []
    # Act
    _report(_unverified_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert emitted[0]["status"] == "start-unverified"


# ---------------------------------------------------------------------------
# verified up — the only path that keeps the legacy success shape.
# ---------------------------------------------------------------------------


def test_verified_up_returns_true(not_in_sif) -> None:
    """A verified launch keeps exit 0."""
    # Arrange
    emitted: list = []
    # Act
    ok = _report(_up_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert ok is True


def test_verified_up_json_status_stays_started(not_in_sif) -> None:
    """Scripts keyed on ``status == "started"`` keep working."""
    # Arrange
    emitted: list = []
    # Act
    _report(_up_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert emitted[0]["status"] == "started"


def test_verified_up_json_carries_the_evidence(not_in_sif) -> None:
    """The success is falsifiable: it says WHAT was observed."""
    # Arrange
    emitted: list = []
    # Act
    _report(_up_verdict(), as_json=True, emitted=emitted)
    # Assert
    assert "first fresh heartbeat" in emitted[0]["verify"]["evidence"]


# ---------------------------------------------------------------------------
# legacy shapes — no-op and dry-run are reported exactly as before.
# ---------------------------------------------------------------------------


def test_noop_keeps_already_running_status(not_in_sif) -> None:
    """The idempotent no-op emits the legacy status untouched."""
    # Arrange
    emitted: list = []
    # Act
    report_start_result(
        _config(),
        noop=True,
        dry_run=False,
        foreground=False,
        one_shot=False,
        as_json=True,
        emit=emitted.append,
        launched_at=time.time(),
        host="test-host",
        host_workdir="/work",
        container_workdir="/work",
        location="test-host@/work:/work",
        verify_fn=_verify_fn_returning(_failed_verdict()),
    )
    # Assert
    assert emitted[0]["status"] == "already_running"


def test_noop_payload_has_no_verify_key(not_in_sif) -> None:
    """Nothing was launched, so no verdict is asserted (back-compat)."""
    # Arrange
    emitted: list = []
    # Act
    report_start_result(
        _config(),
        noop=True,
        dry_run=False,
        foreground=False,
        one_shot=False,
        as_json=True,
        emit=emitted.append,
        launched_at=time.time(),
        host="test-host",
        host_workdir="/work",
        container_workdir="/work",
        location="test-host@/work:/work",
        verify_fn=_verify_fn_returning(_failed_verdict()),
    )
    # Assert
    assert "verify" not in emitted[0]


def test_dry_run_keeps_dry_run_ok_status(not_in_sif) -> None:
    """Dry-run emits the legacy status untouched."""
    # Arrange
    emitted: list = []
    # Act
    report_start_result(
        _config(),
        noop=False,
        dry_run=True,
        foreground=False,
        one_shot=False,
        as_json=True,
        emit=emitted.append,
        launched_at=time.time(),
        host="test-host",
        host_workdir="/work",
        container_workdir="/work",
        location="test-host@/work:/work",
        verify_fn=_verify_fn_returning(_failed_verdict()),
    )
    # Assert
    assert emitted[0]["status"] == "dry_run_ok"
