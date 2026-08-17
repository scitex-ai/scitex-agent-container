#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for peer resolution — config.yaml UNION the host registry.

NO MOCKS. Every test drives the real seam: a real ``hosts.yaml`` written
to a real ``tmp_path`` and read through the real ``$SCITEX_DIR/dev/``
cascade, real ``PeerSpec`` / ``PeersMap`` objects, and the real
``build_ssh_argv`` renderer.

The regression under test was MEASURED on scitex-compute-04 (2026-08-12).
``sac host list`` printed six registry hosts with ssh aliases above an
empty ``peers: []``, and every reachability verb then refused all six::

    $ sac host probe mba
    error: peer 'mba' is not defined in config.yaml

config.yaml does not exist on that host — :func:`host_config.load` is
missing-tolerant by design — so ``cfg.peers`` was ``{}`` and the gate
``peer not in cfg.peers`` rejected everything. sac was reading the
registry for ``scitex_root`` while ignoring the ``ssh_alias`` sitting in
the same row; ``registry_ssh_alias`` had zero call sites. The practical
cost: nothing could answer "which hosts are reachable?" through the
supported CLI, so callers hand-rolled ssh instead.
"""

from __future__ import annotations

import os
import shlex
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

# Import via ``host_config`` (the established public re-export path), not
# the private siblings: ``host_config`` and ``_host_ssh`` form a
# deliberate import cycle, so importing the sibling FIRST hits a
# partially initialized module.
from scitex_agent_container._state._peer_resolve import (
    peers_with_registry,
    registry_peer_names,
)
from scitex_agent_container._state.host_config import (
    PeersMap,
    PeerSpec,
    build_ssh_argv,
)

_HOSTS_YAML = textwrap.dedent(
    """\
    hosts:
      spartan:
        kind: hpc-login
        ssh_alias: spartan
        scitex_root: "/data/gpfs/projects/punim0264/ywatanabe/.scitex"
      mba:
        kind: workstation
        ssh_alias: mba
        scitex_root: "~/.scitex"
      scitex-nas-03:
        kind: storage
        ssh_alias: scitex-nas-03
        scitex_root: "~/.scitex"
      ywata-note-win:
        kind: workstation
        ssh_alias: null
        scitex_root: "~/.scitex"
    """
)

_SPARTAN_PREAMBLE = ("module load Apptainer/1.3.3",)


def _remote_command(argv: list[str]) -> str:
    """The command line ssh actually puts on the REMOTE shell.

    ssh word-joins everything after the host, so ``build_ssh_argv``
    collapses the command into ONE trailing argv element that it quotes
    itself — a bare ``shlex.join`` for a plain peer, or a
    ``bash -c '<preamble> && <cmd>'`` wrapper for a peer carrying an
    ``env_preamble``. Reading the tail back through this helper keeps the
    assertions pinned to what the remote runs, which is the thing these
    tests have always cared about, rather than to the local tokenisation
    (which changed on 2026-08-17 when both branches were made to quote
    exactly once).
    """
    tail = argv[-1]
    if tail.startswith("bash -c "):
        return shlex.split(tail)[2]
    return tail


def _set_scitex_dir(value: str) -> Iterator[None]:
    """Set $SCITEX_DIR for the test, restoring the prior value on teardown."""
    sentinel = object()
    previous: object = os.environ.get("SCITEX_DIR", sentinel)
    os.environ["SCITEX_DIR"] = value
    try:
        yield
    finally:
        if previous is sentinel:
            os.environ.pop("SCITEX_DIR", None)
        else:
            os.environ["SCITEX_DIR"] = str(previous)


@pytest.fixture()
def registry(tmp_path: Path) -> Iterator[Path]:
    """A real hosts.yaml at the real resolved location ($SCITEX_DIR/dev/)."""
    hosts_dir = tmp_path / "dev"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yaml").write_text(_HOSTS_YAML)
    yield from _set_scitex_dir(str(tmp_path))


@pytest.fixture()
def empty_registry(tmp_path: Path) -> Iterator[Path]:
    """A registry file that exists but declares no hosts at all."""
    hosts_dir = tmp_path / "dev"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yaml").write_text("hosts: {}\n")
    yield from _set_scitex_dir(str(tmp_path))


@pytest.fixture()
def glob_peers() -> PeersMap:
    """The two-tier HPC config shape: one pattern key, blank ssh."""
    peers = PeersMap()
    peers["spartan*"] = PeerSpec(
        name="spartan*", ssh="", env_preamble=_SPARTAN_PREAMBLE
    )
    return peers


def test_registry_host_is_routable_with_no_config_peers(registry: Path) -> None:
    """THE bug: no config.yaml must not mean no reachable hosts."""
    # Arrange
    configured: dict[str, PeerSpec] = {}
    # Act
    merged = peers_with_registry(configured)
    # Assert
    assert "mba" in merged


def test_registry_peer_carries_the_declared_ssh_alias(registry: Path) -> None:
    """The route comes from the registry's ``ssh_alias``, verbatim."""
    # Arrange
    configured: dict[str, PeerSpec] = {}
    # Act
    merged = peers_with_registry(configured)
    # Assert
    assert merged["scitex-nas-03"].ssh == "scitex-nas-03"


def test_registry_peer_renders_a_real_ssh_argv(registry: Path) -> None:
    """The resolved peer must be usable by the unmodified ssh renderer.

    A peer that exists ONLY in the host registry — no config.yaml entry
    at all, which is the outage this file was written for — must dispatch
    the caller's command to the remote intact. ``mba``'s registry row
    declares a home-relative ``~/.scitex`` root, so no ``SCITEX_DIR=``
    pin is prepended and the remote line is the command verbatim.

    Assertion shape changed 2026-08-17: the renderer now emits ONE
    shlex-joined trailing element instead of N raw tokens, so the tail is
    read back with :func:`_remote_command`. The property is untouched —
    only the tail's local shape moved.
    """
    # Arrange
    merged = peers_with_registry({})
    # Act
    argv = build_ssh_argv("mba", ["sac", "host", "list", "--json"], merged)
    # Assert
    assert _remote_command(argv) == "sac host list --json"


def test_registry_peer_ssh_argv_targets_the_alias(registry: Path) -> None:
    """The ssh host word is the alias, not the registry row name."""
    # Arrange
    merged = peers_with_registry({})
    # Act
    argv = build_ssh_argv("spartan", ["hostname"], merged)
    # Assert
    assert argv[argv.index("--") - 1] == "spartan"


def test_config_peer_ssh_wins_over_registry_row(registry: Path) -> None:
    """A config entry is explicit operator intent; the registry fills gaps."""
    # Arrange
    configured = {
        "spartan": PeerSpec(name="spartan", ssh="ywatanabe@spartan-login1")
    }
    # Act
    merged = peers_with_registry(configured)
    # Assert
    assert merged["spartan"].ssh == "ywatanabe@spartan-login1"


def test_config_peer_env_preamble_survives_the_merge(registry: Path) -> None:
    """Shadowing a config peer would silently drop its Lmod preamble."""
    # Arrange
    configured = {
        "spartan": PeerSpec(
            name="spartan", ssh="spartan", env_preamble=_SPARTAN_PREAMBLE
        )
    }
    # Act
    merged = peers_with_registry(configured)
    # Assert
    assert merged["spartan"].env_preamble == _SPARTAN_PREAMBLE


def test_config_glob_pattern_is_not_preempted(
    registry: Path, glob_peers: PeersMap
) -> None:
    """A registry exact row must not steal a name a config glob covers.

    ``PeersMap`` prefers an exact key over a pattern, so injecting an
    exact ``spartan`` row would strip the ``spartan*`` entry's
    env_preamble — the ``module load`` that puts ``apptainer`` on
    Spartan's PATH.
    """
    # Arrange
    expected = _SPARTAN_PREAMBLE
    # Act
    merged = peers_with_registry(glob_peers)
    # Assert
    assert merged["spartan"].env_preamble == expected


def test_glob_matched_peer_still_fills_ssh_from_the_queried_name(
    registry: Path, glob_peers: PeersMap
) -> None:
    """The pattern's blank ssh keeps resolving to the queried name."""
    # Arrange
    expected = "spartan"
    # Act
    merged = peers_with_registry(glob_peers)
    # Assert
    assert merged["spartan"].ssh == expected


def test_row_without_ssh_alias_is_not_routable(registry: Path) -> None:
    """``ywata-note-win`` records ``ssh_alias: null`` deliberately.

    Inbound ssh to it times out, so it has no route; offering it would
    render an ssh argv with an empty host and fail unreadably.
    """
    # Arrange
    configured: dict[str, PeerSpec] = {}
    # Act
    merged = peers_with_registry(configured)
    # Assert
    assert "ywata-note-win" not in merged


def test_registry_peer_names_reports_the_filled_gaps(registry: Path) -> None:
    # Arrange
    configured = {"mba": PeerSpec(name="mba", ssh="mba.local")}
    # Act
    names = registry_peer_names(configured)
    # Assert
    assert {"spartan", "scitex-nas-03"} <= names


def test_registry_peer_names_excludes_configured_peers(registry: Path) -> None:
    """A name the operator configured is labelled config, not registry."""
    # Arrange
    configured = {"mba": PeerSpec(name="mba", ssh="mba.local")}
    # Act
    names = registry_peer_names(configured)
    # Assert
    assert "mba" not in names


def test_config_peer_survives_when_registry_is_empty(empty_registry: Path) -> None:
    """A box with no registry hosts keeps its pre-existing config peers."""
    # Arrange
    configured = {"bm198": PeerSpec(name="bm198", ssh="bm198", via=("spartan",))}
    # Act
    merged = peers_with_registry(configured)
    # Assert
    assert merged["bm198"].ssh == "bm198"


def test_config_peer_via_chain_survives_when_registry_is_empty(
    empty_registry: Path,
) -> None:
    """The ProxyJump chain is part of that peer and must not be rebuilt."""
    # Arrange
    configured = {"bm198": PeerSpec(name="bm198", ssh="bm198", via=("spartan",))}
    # Act
    merged = peers_with_registry(configured)
    # Assert
    assert merged["bm198"].via == ("spartan",)


# EOF
