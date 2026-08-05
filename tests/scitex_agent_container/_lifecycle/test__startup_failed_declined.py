"""Tests for the P1 decline guard: a `--yes`-less refusal must never mint a
STARTUP_FAILED marker.

``write_marker`` gains a guard matching the shared
``_start_decline.DECLINE_SENTINEL``; the refusal branch in
``cli_pkg/lifecycle/_start_single.py`` emits it. Covers both ends: the
guard itself (direct ``write_marker`` calls) and the emitter (a real
interactive refusal through ``run_single_targets``).
"""

from __future__ import annotations

import json
import logging
import os
import pty
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest

from scitex_agent_container._lifecycle._start_decline import DECLINE_SENTINEL
from scitex_agent_container._lifecycle._startup_failed import (
    MARKER_FILENAME,
    write_marker,
)
from scitex_agent_container.cli_pkg.lifecycle._start_single import run_single_targets
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

# ---------------------------------------------------------------------------
# write_marker guard — direct calls, no CLI
# ---------------------------------------------------------------------------

_DECLINE_STDERR = (
    "refusing to start x without --yes/-y — the plan above shows exactly "
    f"what will mount and run; re-run with --yes to launch.\n{DECLINE_SENTINEL}\n"
)


def test_write_marker_returns_none_for_a_declined_start(tmp_path: Path) -> None:
    # Arrange
    kwargs = dict(
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=1,
        stdout="",
        stderr=_DECLINE_STDERR,
    )
    # Act
    target = write_marker(tmp_path, **kwargs)
    # Assert
    assert target is None


def test_write_marker_writes_no_file_for_a_declined_start(tmp_path: Path) -> None:
    # Arrange
    kwargs = dict(
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=1,
        stdout="",
        stderr=_DECLINE_STDERR,
    )
    # Act
    write_marker(tmp_path, **kwargs)
    # Assert
    assert not (tmp_path / MARKER_FILENAME).exists()


_REAL_FAILURE_STDERR = (
    "FATAL: container creation failed: mount source /work/x doesn't exist"
)


def test_write_marker_returns_a_path_for_a_real_failure(tmp_path: Path) -> None:
    # Arrange
    kwargs = dict(
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr=_REAL_FAILURE_STDERR,
    )
    # Act
    target = write_marker(tmp_path, **kwargs)
    # Assert
    assert target is not None


def test_write_marker_creates_the_file_for_a_real_failure(tmp_path: Path) -> None:
    # Arrange
    kwargs = dict(
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr=_REAL_FAILURE_STDERR,
    )
    # Act
    write_marker(tmp_path, **kwargs)
    # Assert
    assert (tmp_path / MARKER_FILENAME).is_file()


def test_write_marker_classifies_a_real_apptainer_failure(tmp_path: Path) -> None:
    # Arrange
    kwargs = dict(
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr=_REAL_FAILURE_STDERR,
    )
    target = write_marker(tmp_path, **kwargs)
    # Act
    payload = json.loads(target.read_text())
    # Assert
    assert payload["kind"] == "apptainer_mount_failed"


_CLIPPED_DECLINE_STDERR = (
    "x" * 32_000
) + f"\nrefusing to start x without --yes/-y\n{DECLINE_SENTINEL}\n"


def test_sentinel_survives_the_marker_tail_clip_return_value(tmp_path: Path) -> None:
    # Arrange — 32 KiB of noise, sentinel at the very end. ``_tail_text``
    # clips the stored payload to 8 KiB, but the guard must see the FULL
    # text, not the clipped tail.
    kwargs = dict(
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=1,
        stdout="",
        stderr=_CLIPPED_DECLINE_STDERR,
    )
    # Act
    target = write_marker(tmp_path, **kwargs)
    # Assert
    assert target is None


def test_sentinel_survives_the_marker_tail_clip_no_file(tmp_path: Path) -> None:
    # Arrange
    kwargs = dict(
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=1,
        stdout="",
        stderr=_CLIPPED_DECLINE_STDERR,
    )
    # Act
    write_marker(tmp_path, **kwargs)
    # Assert
    assert not (tmp_path / MARKER_FILENAME).exists()


# ---------------------------------------------------------------------------
# The refusal branch emits the sentinel — real interactive run_single_targets
# ---------------------------------------------------------------------------


@contextmanager
def _real_tty_stdin() -> Iterator[None]:
    """Swap ``sys.stdin`` for a REAL pty slave — ``isatty()`` genuinely True."""
    master_fd, slave_fd = pty.openpty()
    saved_stdin = sys.stdin
    slave_file = os.fdopen(slave_fd, "r")
    sys.stdin = slave_file
    try:
        yield
    finally:
        sys.stdin = saved_stdin
        slave_file.close()
        os.close(master_fd)


def _write_local_spec(home: Path, name: str) -> Path:
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True)
    yaml_path = agents_dir / f"{name}.yaml"
    yaml_path.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            'metadata:\n  labels:\n    sac-builtin: "off"\n'
            "spec:\n"
            "  runtime: apptainer\n"
            "  host: ${HOSTNAME}\n"
            "  workdir: /home/agent/work\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  claude:\n    model: sonnet\n"
            "  health:\n    enabled: true\n    interval: 60\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
            "  a2a:\n    port: null\n"
        )
    )
    return yaml_path


class _Refusal(NamedTuple):
    exit_code: int | None
    stderr: str
    log_text: str


def _refuse(tmp_path: Path, env_save_restore, capsys, caplog) -> _Refusal:
    env_save_restore.set("HOME", str(tmp_path))
    yaml_path = _write_local_spec(tmp_path, "alpha")
    caplog.set_level(logging.DEBUG)
    with _real_tty_stdin():
        with pytest.raises(SystemExit) as exc_info:
            run_single_targets(
                [str(yaml_path)],
                no_preflight=True,
                force=False,
                resume_id=None,
                session_mode=None,
                dry_run=False,
                as_json=False,
                foreground=False,
                one_shot=False,
                strict_drift=False,
                no_redispatch=True,
                multi_foreground=False,
                preflight_runner=lambda: None,
                yes=False,
            )
    return _Refusal(
        exit_code=exc_info.value.code,
        stderr=capsys.readouterr().err,
        log_text=caplog.text,
    )


def test_the_refusal_branch_exits_1(tmp_path, env_save_restore, capsys, caplog) -> None:
    # Arrange
    # (fixtures above build the isolated HOME + real-tty seam)
    # Act
    result = _refuse(tmp_path, env_save_restore, capsys, caplog)
    # Assert
    assert result.exit_code == 1


def test_the_refusal_branch_emits_the_shared_sentinel(
    tmp_path, env_save_restore, capsys, caplog
) -> None:
    # Arrange
    # (fixtures above build the isolated HOME + real-tty seam)
    # Act
    result = _refuse(tmp_path, env_save_restore, capsys, caplog)
    # Assert
    assert DECLINE_SENTINEL in result.stderr


def test_the_refusal_branch_still_logs_the_yes_hint(
    tmp_path, env_save_restore, capsys, caplog
) -> None:
    # Arrange
    # (fixtures above build the isolated HOME + real-tty seam)
    # Act
    result = _refuse(tmp_path, env_save_restore, capsys, caplog)
    # Assert
    assert "without --yes/-y" in result.log_text
