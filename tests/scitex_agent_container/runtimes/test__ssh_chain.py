"""Tests for ssh hop-chain rendering helpers.

Covers argv shape of :func:`render_ssh_chain`, the local-hop trimming
in :func:`skip_local_hops` (mocking ``is_local_host`` so the result is
deterministic on every dev box), and the full :func:`build_ssh_command`
wrapper including the no-hop / empty-list short-circuit.
"""

from __future__ import annotations

from unittest.mock import patch

from scitex_agent_container.runtimes._ssh_chain import (
    build_ssh_command,
    render_ssh_chain,
    skip_local_hops,
)

# --- render_ssh_chain ---------------------------------------------------


def test_render_empty_returns_empty():
    assert render_ssh_chain([]) == []


def test_render_single_hop_is_bare_host():
    assert render_ssh_chain(["alpha"]) == ["alpha"]


def test_render_two_hops_uses_J_flag():
    # Two hops → first is the jump, last is the terminal.
    assert render_ssh_chain(["hop1", "target"]) == ["-J", "hop1", "target"]


def test_render_many_hops_joins_with_commas():
    assert render_ssh_chain(["h1", "h2", "h3", "final"]) == [
        "-J",
        "h1,h2,h3",
        "final",
    ]


# --- skip_local_hops ----------------------------------------------------


def test_skip_local_hops_drops_leading_local():
    with patch(
        "scitex_agent_container.runtimes._ssh_chain.is_local_host",
        side_effect=lambda h: h == "spartan",
    ):
        assert skip_local_hops(["spartan", "spartan-bm149"]) == ["spartan-bm149"]


def test_skip_local_hops_passes_through_remote_only():
    with patch(
        "scitex_agent_container.runtimes._ssh_chain.is_local_host",
        return_value=False,
    ):
        assert skip_local_hops(["a", "b", "c"]) == ["a", "b", "c"]


def test_skip_local_hops_all_local_yields_empty():
    with patch(
        "scitex_agent_container.runtimes._ssh_chain.is_local_host",
        return_value=True,
    ):
        assert skip_local_hops(["spartan"]) == []
        assert skip_local_hops(["a", "b"]) == []


def test_skip_local_hops_stops_at_first_remote():
    # Only LEADING locals are trimmed; a local hop after a remote one
    # must survive (otherwise the chain semantics break).
    seq = ["local-a", "remote-b", "local-c"]
    with patch(
        "scitex_agent_container.runtimes._ssh_chain.is_local_host",
        side_effect=lambda h: h.startswith("local"),
    ):
        assert skip_local_hops(seq) == ["remote-b", "local-c"]


def test_skip_local_hops_does_not_mutate_input():
    inp = ["spartan", "target"]
    with patch(
        "scitex_agent_container.runtimes._ssh_chain.is_local_host",
        side_effect=lambda h: h == "spartan",
    ):
        skip_local_hops(inp)
    assert inp == ["spartan", "target"]


# --- build_ssh_command --------------------------------------------------


def test_build_ssh_command_empty_returns_none():
    assert build_ssh_command([], "echo ok") is None


def test_build_ssh_command_single_hop_no_opts():
    cmd = build_ssh_command(["host"], "echo ok")
    assert cmd == ["ssh", "host", "echo ok"]


def test_build_ssh_command_with_opts_preserves_order():
    cmd = build_ssh_command(["host"], "uptime", ssh_opts=["-o", "BatchMode=yes"])
    assert cmd == ["ssh", "-o", "BatchMode=yes", "host", "uptime"]


def test_build_ssh_command_multi_hop_emits_J_flag():
    cmd = build_ssh_command(["hop1", "hop2", "final"], "hostname")
    assert cmd == ["ssh", "-J", "hop1,hop2", "final", "hostname"]


def test_build_ssh_command_multi_hop_with_opts():
    cmd = build_ssh_command(
        ["hop1", "final"], "id", ssh_opts=["-o", "ConnectTimeout=10"]
    )
    assert cmd == ["ssh", "-o", "ConnectTimeout=10", "-J", "hop1", "final", "id"]
