"""Regression tests for the runner state-dir atomic write helper.

The bug: the runner persisted small state files with a FIXED ``<name>.tmp``
sibling + ``os.replace``. Two processes sharing one state dir (two xdist
workers keyed on the same agent name, or two runner processes racing on one
agent) both open the SAME tmp; the first rename consumes it and the loser's
``replace()`` raises ``FileNotFoundError``. Measured on CI as
``.../runtime/alpha/instance_id.tmp -> .../runtime/alpha/instance_id``.

``atomic_write_text`` gives each writer a UNIQUE tmp (via ``mkstemp``), so
only the final atomic rename contends and nothing raises. The concurrency
test fails on the pre-fix code and passes on the fix.
"""

from __future__ import annotations

import threading

from scitex_agent_container._runners._atomic import atomic_write_text
from scitex_agent_container._runners._session_state import (
    read_instance_id,
    write_instance_id,
)


def test_concurrent_writers_sharing_a_dir_do_not_raise(tmp_path):
    """Eight writers hammering one target must never raise the tmp race.

    On the pre-fix fixed-tmp code this raises FileNotFoundError with high
    probability; the unique-tmp helper is collision-free by construction.
    """
    # Arrange
    dst = tmp_path / "alpha" / "instance_id"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        start.wait()
        try:
            for i in range(200):
                atomic_write_text(dst, f"id-{n}-{i}")
        except OSError as exc:  # the race surfaces as FileNotFoundError
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]

    # Act
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Assert
    assert not errors, f"concurrent writers raced: {errors[:3]}"


def test_write_persists_the_value(tmp_path):
    """A completed write leaves the exact bytes at the target."""
    # Arrange
    dst = tmp_path / "pid"

    # Act
    atomic_write_text(dst, "123\n")

    # Assert
    assert dst.read_text() == "123\n"


def test_write_leaves_no_stray_tmp_sibling(tmp_path):
    """The unique tmp is renamed away, not left behind."""
    # Arrange
    dst = tmp_path / "pid"

    # Act
    atomic_write_text(dst, "123\n")

    # Assert
    assert [p.name for p in tmp_path.iterdir()] == ["pid"]


def test_write_creates_missing_parent_dir(tmp_path):
    """The helper creates ``dst.parent`` if absent (as the old sites did)."""
    # Arrange
    dst = tmp_path / "deep" / "nested" / "started_at"

    # Act
    atomic_write_text(dst, "1700000000.0")

    # Assert
    assert dst.read_text() == "1700000000.0"


def test_write_instance_id_round_trips(tmp_path):
    """The measured-failure caller round-trips through the unique-tmp helper."""
    # Arrange
    state_dir = tmp_path / "alpha"

    # Act
    write_instance_id(state_dir, "uuid-7-abc")

    # Assert
    assert read_instance_id(state_dir) == "uuid-7-abc"


def test_write_instance_id_overwrite_leaves_no_tmp(tmp_path):
    """Overwriting the latest marker leaves no ``.tmp`` behind."""
    # Arrange
    state_dir = tmp_path / "alpha"
    write_instance_id(state_dir, "uuid-7-abc")

    # Act
    write_instance_id(state_dir, "uuid-7-def")

    # Assert
    assert not list(state_dir.glob("*.tmp"))
