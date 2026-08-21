"""The thread-leak gate's own regression tests — can it actually fire?

A gate that has never been observed failing is indistinguishable from one
that cannot fail, and the whole suite would then read green forever. These
drive `leaked_threads` against a thread this file starts and then JOINS, so
nothing escapes into the worker and the autouse gate stays quiet.
"""

from __future__ import annotations

import threading

from tests.scitex_agent_container._helpers.thread_leak import (
    _ALLOWED_NAME_PREFIXES,
    _describe,
    _is_allowed,
    leaked_threads,
)


def _snapshot() -> set[int]:
    return {t.ident for t in threading.enumerate() if t.ident is not None}


def test_a_still_running_thread_is_reported():
    # Arrange — POSITIVE CONTROL: a live thread the detector must see
    stop = threading.Event()
    before = _snapshot()
    worker = threading.Thread(target=stop.wait, name="leak-canary", daemon=True)
    worker.start()

    # Act
    try:
        found = [t.name for t in leaked_threads(before)]
    finally:
        stop.set()
        worker.join(timeout=5)

    # Assert
    assert "leak-canary" in found


def test_a_finished_thread_is_not_reported():
    # Arrange — the counterpart: a thread that ended is not a leak
    stop = threading.Event()
    before = _snapshot()
    worker = threading.Thread(target=stop.wait, name="tidy-canary", daemon=True)
    worker.start()
    stop.set()
    worker.join(timeout=5)

    # Act
    found = [t.name for t in leaked_threads(before)]

    # Assert
    assert "tidy-canary" not in found


def test_a_thread_present_before_the_test_is_not_reported():
    # Arrange — only NEW threads count; the worker's own are not the test's fault
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name="pre-existing", daemon=True)
    worker.start()
    before = _snapshot()

    # Act
    try:
        found = [t.name for t in leaked_threads(before)]
    finally:
        stop.set()
        worker.join(timeout=5)

    # Assert
    assert "pre-existing" not in found


def test_an_allowlisted_thread_is_not_reported():
    # Arrange — the hooks pool's prefix must survive; it is deliberate
    stop = threading.Event()
    before = _snapshot()
    worker = threading.Thread(target=stop.wait, name="scitex-hook_0", daemon=True)
    worker.start()

    # Act
    try:
        found = [t.name for t in leaked_threads(before)]
    finally:
        stop.set()
        worker.join(timeout=5)

    # Assert
    assert "scitex-hook_0" not in found


def test_the_hooks_pool_prefix_is_the_one_hooks_actually_uses():
    # Arrange — the allowlist is worthless if it names a prefix nothing sets.
    # Read it from the source of truth rather than restating the literal.
    from scitex_agent_container import hooks

    # Act
    prefix = hooks._POOL._thread_name_prefix

    # Assert — a real pool thread must satisfy the allowlist
    assert _is_allowed(threading.Thread(name=f"{prefix}_0"))


def test_every_allowlist_entry_is_non_empty():
    # Arrange — an empty prefix would allowlist EVERY thread and silently
    # disable the gate while every test kept passing
    entries = _ALLOWED_NAME_PREFIXES

    # Act
    empty = [p for p in entries if not p.strip()]

    # Assert
    assert empty == []


def test_the_message_names_the_target_not_just_the_thread():
    # Arrange — "Thread-7 daemon=True" alone tells a reader nothing about
    # WHICH code leaked; the target is what makes the failure actionable
    def _sentinel_target():
        return None

    worker = threading.Thread(target=_sentinel_target, name="described")

    # Act
    rendered = _describe(worker)

    # Assert
    assert "_sentinel_target" in rendered
