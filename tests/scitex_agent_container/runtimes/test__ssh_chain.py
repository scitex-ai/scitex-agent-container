"""Tests for ssh hop-chain rendering helpers.

Covers argv shape of :func:`render_ssh_chain`, the local-hop trimming
in :func:`skip_local_hops` (with a hand-rolled fake ``is_local_host``
so the result is deterministic on every dev box), and the full
:func:`build_ssh_command` wrapper including the no-hop / empty-list
short-circuit.

PA-306: no ``unittest.mock``. ``is_local_host`` is swapped at module
level via a small ``_swap_locality`` context manager — same effect as
``patch`` but no banned imports or fixtures.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

import pytest

import scitex_agent_container.runtimes._ssh_chain as ssh_chain_mod
from scitex_agent_container.runtimes._ssh_chain import (
    build_ssh_command,
    render_ssh_chain,
    skip_local_hops,
)


@contextmanager
def _swap_locality(fn: Callable[[str], bool]) -> Iterator[None]:
    saved = ssh_chain_mod.is_local_host
    ssh_chain_mod.is_local_host = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        ssh_chain_mod.is_local_host = saved  # type: ignore[assignment]


# --- render_ssh_chain ---------------------------------------------------


@pytest.mark.parametrize(
    "hops, expected",
    [
        ([], []),
        (["alpha"], ["alpha"]),
        (["hop1", "target"], ["-J", "hop1", "target"]),
        (["h1", "h2", "h3", "final"], ["-J", "h1,h2,h3", "final"]),
    ],
    ids=[
        "empty_returns_empty",
        "single_hop_is_bare_host",
        "two_hops_uses_J_flag",
        "many_hops_joins_with_commas",
    ],
)
def test_render_ssh_chain_shapes_argv(hops, expected):
    # Arrange
    chain = list(hops)
    # Act
    rendered = render_ssh_chain(chain)
    # Assert
    assert rendered == expected


# --- skip_local_hops ----------------------------------------------------


def test_skip_local_hops_drops_leading_local():
    # Arrange
    chain = ["spartan", "spartan-bm149"]
    # Act
    with _swap_locality(lambda h: h == "spartan"):
        result = skip_local_hops(chain)
    # Assert
    assert result == ["spartan-bm149"]


def test_skip_local_hops_passes_through_remote_only():
    # Arrange
    chain = ["a", "b", "c"]
    # Act
    with _swap_locality(lambda _h: False):
        result = skip_local_hops(chain)
    # Assert
    assert result == ["a", "b", "c"]


@pytest.mark.parametrize(
    "chain",
    [["spartan"], ["a", "b"]],
    ids=["single_local", "two_locals"],
)
def test_skip_local_hops_all_local_yields_empty(chain):
    # Arrange
    inp = list(chain)
    # Act
    with _swap_locality(lambda _h: True):
        result = skip_local_hops(inp)
    # Assert
    assert result == []


def test_skip_local_hops_stops_at_first_remote():
    # Only LEADING locals are trimmed; a local hop after a remote one
    # must survive (otherwise the chain semantics break).
    # Arrange
    seq = ["local-a", "remote-b", "local-c"]
    # Act
    with _swap_locality(lambda h: h.startswith("local")):
        result = skip_local_hops(seq)
    # Assert
    assert result == ["remote-b", "local-c"]


def test_skip_local_hops_does_not_mutate_input():
    # Arrange
    inp = ["spartan", "target"]
    # Act
    with _swap_locality(lambda h: h == "spartan"):
        skip_local_hops(inp)
    # Assert
    assert inp == ["spartan", "target"]


# --- build_ssh_command --------------------------------------------------


def test_build_ssh_command_empty_returns_none():
    # Arrange
    hops: list[str] = []
    # Act
    cmd = build_ssh_command(hops, "echo ok")
    # Assert
    assert cmd is None


def test_build_ssh_command_single_hop_no_opts():
    # Arrange
    hops = ["host"]
    # Act
    cmd = build_ssh_command(hops, "echo ok")
    # Assert
    assert cmd == ["ssh", "host", "echo ok"]


def test_build_ssh_command_with_opts_preserves_order():
    # Arrange
    hops = ["host"]
    # Act
    cmd = build_ssh_command(hops, "uptime", ssh_opts=["-o", "BatchMode=yes"])
    # Assert
    assert cmd == ["ssh", "-o", "BatchMode=yes", "host", "uptime"]


def test_build_ssh_command_multi_hop_emits_J_flag():
    # Arrange
    hops = ["hop1", "hop2", "final"]
    # Act
    cmd = build_ssh_command(hops, "hostname")
    # Assert
    assert cmd == ["ssh", "-J", "hop1,hop2", "final", "hostname"]


def test_build_ssh_command_multi_hop_with_opts():
    # Arrange
    hops = ["hop1", "final"]
    # Act
    cmd = build_ssh_command(hops, "id", ssh_opts=["-o", "ConnectTimeout=10"])
    # Assert
    assert cmd == ["ssh", "-o", "ConnectTimeout=10", "-J", "hop1", "final", "id"]
