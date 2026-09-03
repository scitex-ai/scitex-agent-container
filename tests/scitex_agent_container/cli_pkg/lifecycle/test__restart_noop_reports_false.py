#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A restart that no-op'd must report ``restarted: false``.

Incident 2026-07-12. ``POST /agents/scitex-storage/restart`` answered::

    {"name": "scitex-storage", "restarted": true, "dispatched": false}

with rc=0, while the underlying start leg had logged::

    Agent 'scitex-storage' is already running. No-op. Use --force to restart.

Process evidence confirmed nothing cycled (same claude pid). A caller
counting rc=0 as success marks an unrestarted agent as rolled — and then
diagnoses the wrong subsystem when the agent keeps answering on its OLD,
stale credentials.

The envelope is built in ``_restart_one``; ``restarted`` came from
``agent_restart(name) is not False``, and the start leg's idempotent
no-op satisfies ``is not False`` while having launched NOTHING. These
tests pin the honest contract: ``restarted`` is true ONLY when the
process actually cycled, and a no-op names itself via ``reason``.

PA-306: no ``unittest.mock`` / ``monkeypatch``. Production collaborators
are swapped at the module namespace with an explicit save/restore
``_swap``, matching ``test__restart.py``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Callable, Iterator

import pytest

import scitex_agent_container.cli_pkg.lifecycle._restart as restart_mod
import scitex_agent_container.cli_pkg.lifecycle._restart_local as restart_local_mod
from scitex_agent_container._lifecycle._start_outcome import (
    KIND_ALREADY_RUNNING,
    NOOP_ALREADY_RUNNING,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path):
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@contextmanager
def _swap(name: str, fn: Callable) -> Iterator[None]:
    # The local restart leg moved into ``_restart_local`` (v4 step 5
    # split) and reads its collaborators from ITS OWN globals; swap on
    # whichever of the two modules carries the name (same shape as
    # ``test__restart.py``'s harness).
    targets = [m for m in (restart_mod, restart_local_mod) if hasattr(m, name)]
    saved_pairs = [(m, getattr(m, name)) for m in targets]
    for m in targets:
        setattr(m, name, fn)
    try:
        yield
    finally:
        for m, value in saved_pairs:
            setattr(m, name, value)


@contextmanager
def _local_restart(result) -> Iterator[None]:
    """Pin the LOCAL restart path and make its start leg return ``result``.

    The incident envelope carried ``dispatched: false`` and no ``via``
    key, which identifies the pure-local branch — so both remote routes
    are stood down here.
    """
    with (
        _swap("try_dispatch_remote", lambda *a, **k: False),
        _swap("spec_host_fallback_peer", lambda *a, **k: None),
        _swap("agent_restart", lambda name, **_kw: result),
    ):
        yield


class TestNoOpRestartIsReportedAsFailure:
    """The exact shape from the incident must now come back honest."""

    def test_no_op_restart_reports_restarted_false(self):
        # Arrange: the start leg no-ops over a live agent, returning the
        # TAGGED-but-truthy sentinel exactly as ``_start.py`` now does.
        # Act
        with _local_restart(NOOP_ALREADY_RUNNING):
            envelope, _ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert: THE regression guard. Pre-fix this key was ``True``.
        assert envelope["restarted"] is False

    def test_no_op_restart_reports_not_ok_so_exit_code_is_nonzero(self):
        # Arrange
        # Act: the second tuple element drives ``any_failed`` -> exit(1).
        with _local_restart(NOOP_ALREADY_RUNNING):
            _envelope, ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert: a caller counting rc=0 as success is no longer lied to.
        assert ok is False

    def test_no_op_restart_names_the_reason(self):
        # Arrange
        # Act
        with _local_restart(NOOP_ALREADY_RUNNING):
            envelope, _ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert: ``restarted: false`` with no cause reads as the generic
        # start-leg failure and sends the operator to the wrong recovery.
        assert envelope["reason"] == KIND_ALREADY_RUNNING

    def test_no_op_restart_hint_tells_the_operator_how_to_cycle_it(self):
        # Arrange
        # Act
        with _local_restart(NOOP_ALREADY_RUNNING):
            envelope, _ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert
        assert "--force" in envelope["hint"]


class TestRealRestartStillReportsSuccess:
    """The fix must not invent the mirror-image lie (a false FAILURE)."""

    def test_real_restart_reports_restarted_true(self):
        # Arrange: a genuine cycle returns plain ``True`` (no kind tag).
        # Act
        with _local_restart(True):
            envelope, _ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert
        assert envelope["restarted"] is True

    def test_real_restart_carries_no_reason_key(self):
        # Arrange
        # Act
        with _local_restart(True):
            envelope, _ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert: ``reason`` appears ONLY on the no-op path.
        assert "reason" not in envelope

    def test_runtime_returning_none_is_not_read_as_failure(self):
        # Arrange: ``agent_start`` forwards ``runtime.start(...)`` on one
        # path, and a runtime returning None must NOT be read as "restart
        # failed" — that is the deliberate ``is not False`` semantics.
        # Act
        with _local_restart(None):
            envelope, _ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert
        assert envelope["restarted"] is True

    def test_explicit_false_still_reports_failure(self):
        # Arrange
        # Act
        with _local_restart(False):
            envelope, _ok = restart_mod._restart_one(
                "victim", as_json=True, fresh=False
            )
        # Assert
        assert envelope["restarted"] is False
