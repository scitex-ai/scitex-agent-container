"""Tests for cli_pkg.lifecycle._selection — the SHARED bulk-selection surface.

PA-306: no ``unittest.mock``. The one collaborator these enumerators have
(``get_agent_list_data``) is swapped at the ``_helpers`` module namespace via a
small ``_swap`` context manager — the same seam ``test__restart`` / ``test__stop``
use for theirs.

Those two files swap ``_enumerate_running`` itself, so they cover the *plumbing*
(which flag reaches which seam) and never the enumeration's own filter. This file
covers that filter — in particular the rule that ``auth-failed`` is LIVE.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Callable, Iterator

import pytest

import scitex_agent_container.cli_pkg._helpers as helpers_mod
from scitex_agent_container.cli_pkg.lifecycle._selection import (
    _enumerate_fleet,
    _enumerate_running,
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
    """Swap an attribute on the ``_helpers`` namespace both enumerators read.

    Both functions resolve ``get_agent_list_data`` from ``.._helpers`` at CALL
    time, so a module-namespace swap is the real seam (``_helpers`` caches its
    PEP-562 lazy attrs into module globals, which this shadows).
    """
    saved = getattr(helpers_mod, name)
    setattr(helpers_mod, name, fn)
    try:
        yield
    finally:
        setattr(helpers_mod, name, saved)


def _rows(*pairs: tuple[str, str]) -> Callable:
    """A ``get_agent_list_data`` stand-in returning ``(name, status)`` rows."""
    data = [{"name": n, "status": s} for n, s in pairs]
    return lambda _registry: data


def test_all_running_includes_an_auth_failed_agent():
    # Arrange — the whole point of the feature: a tmux-green agent whose Claude
    # is auth-dead is UP, so it belongs to the live set. Were it filtered out as
    # "not running", `restart --all-running` would skip precisely the agent a
    # restart cures.
    rows = _rows(("green", "running"), ("wedged", "auth-failed"))
    # Act
    with _swap("get_agent_list_data", rows):
        names = _enumerate_running()
    # Assert
    assert names == ["green", "wedged"]


def test_all_running_still_excludes_the_non_live_statuses():
    # Arrange — an agent the operator deliberately stopped must stay stopped.
    rows = _rows(
        ("live", "running"),
        ("halted", "stopped"),
        ("ghost", "unknown"),
        ("on-disk", "defined"),
        ("broken", "invalid"),
    )
    # Act
    with _swap("get_agent_list_data", rows):
        names = _enumerate_running()
    # Assert
    assert names == ["live"]


def test_all_running_dedups_preserving_first_seen_order():
    # Arrange — the same agent can surface from more than one source row.
    rows = _rows(
        ("b", "auth-failed"),
        ("a", "running"),
        ("b", "running"),
    )
    # Act
    with _swap("get_agent_list_data", rows):
        names = _enumerate_running()
    # Assert
    assert names == ["b", "a"]


def test_all_registry_returns_every_agent_including_stopped_ones():
    # Arrange — --all-registry is "everything `sac agents list` shows".
    rows = _rows(("live", "running"), ("wedged", "auth-failed"), ("halted", "stopped"))
    # Act
    with _swap("get_agent_list_data", rows):
        names = _enumerate_fleet()
    # Assert
    assert names == ["live", "wedged", "halted"]


# #648's invariant: the two destructive verbs import the SAME selection surface
# so they cannot drift. Identity, not equality — a copy would let one verb's
# liveness rule (and so its auth-failed handling) diverge from the other's.


def test_restart_shares_the_selection_running_enumerator():
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle import _restart

    # Act
    used = _restart._enumerate_running
    # Assert
    assert used is _enumerate_running


def test_stop_shares_the_selection_running_enumerator():
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle import _stop

    # Act
    used = _stop._enumerate_running
    # Assert
    assert used is _enumerate_running


def test_restart_shares_the_selection_fleet_enumerator():
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle import _restart

    # Act
    used = _restart._enumerate_fleet
    # Assert
    assert used is _enumerate_fleet


def test_stop_shares_the_selection_fleet_enumerator():
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle import _stop

    # Act
    used = _stop._enumerate_fleet
    # Assert
    assert used is _enumerate_fleet
