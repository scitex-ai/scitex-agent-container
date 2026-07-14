#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the scitex-dev host-registry adapter (the SSOT port).

NO MOCKS, no monkeypatch. Every test drives the real seam: a real
``hosts.yaml`` written to a real ``tmp_path``, read through the real
resolution cascade (``$SCITEX_DIR/dev/hosts.yaml``, via a yield-based env
fixture that restores the environment on teardown), and rendered by the
real ``build_ssh_argv`` with real ``PeerSpec`` objects.

The regression under test is concrete and was MEASURED on Spartan
(2026-07-14), not imagined: the registry declares
``spartan.scitex_root = /data/gpfs/projects/punim0264/ywatanabe/.scitex``
while the remote's ``~/.scitex`` is a symlink into an unrelated paper
project. Any consumer that follows ``~/.scitex`` on the remote writes the
fleet's state into that project — which is exactly what happened (1.6GB
of sac state, including a 1.41GB SIF, landed there). These tests pin the
fix: sac resolves the root from the registry and hands it to the remote
EXPLICITLY, and never expands ``~`` on the local machine.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

# Import via ``host_config`` (the established public re-export path), NOT
# ``_host_ssh`` directly: the two modules form a deliberate import cycle
# (host_config defines PeerSpec, then re-exports the renderer at the
# bottom), so importing the private sibling FIRST hits a partially
# initialized module.
from scitex_agent_container._state.host_config import (
    PeerSpec,
    build_ssh_argv,
    resolve_peer_scitex_root,
)
from scitex_agent_container._state.host_registry import (
    registry_hosts,
    registry_scitex_root,
    remote_state_root,
)

SPARTAN_ROOT = "/data/gpfs/projects/punim0264/ywatanabe/.scitex"

_HOSTS_YAML = textwrap.dedent(
    f"""\
    hosts:
      spartan:
        kind: hpc-login
        ssh_alias: spartan
        scitex_root: "{SPARTAN_ROOT}"
      mba:
        kind: workstation
        ssh_alias: mba
        scitex_root: "~/.scitex"
    """
)


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
def registry_without_spartan(tmp_path: Path) -> Iterator[Path]:
    """A registry that EXISTS but does not know the peer we dispatch to.

    This — not "no registry at all" — is the reachable degradation path.
    ``scitex_dev.hosts.list_hosts()`` SEEDS ``$SCITEX_DIR/dev/hosts.yaml``
    with the operator's known hosts when the file is absent (a read that
    writes — verified 2026-07-14), so "no registry" resolves to "the
    seeded default registry", never to nothing.
    """
    hosts_dir = tmp_path / "dev"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yaml").write_text(
        textwrap.dedent(
            """\
            hosts:
              mba:
                kind: workstation
                ssh_alias: mba
                scitex_root: "~/.scitex"
            """
        )
    )
    yield from _set_scitex_dir(str(tmp_path))


def _peers() -> dict[str, PeerSpec]:
    """Real peer specs mirroring the fleet's config.yaml shape."""
    return {
        "spartan": PeerSpec(name="spartan", ssh="spartan"),
        # Two-tier HPC target: a compute node fronted by the login node.
        "spartan-bm043": PeerSpec(
            name="spartan-bm043",
            ssh="spartan-bm043",
            via=("spartan",),
            env_preamble=("module load Apptainer/1.3.3",),
        ),
        "mba": PeerSpec(name="mba", ssh="mba"),
    }


def _remote_cmd(argv: list[str]) -> list[str]:
    """The command portion of an ssh argv (everything past the ``--``)."""
    return argv[argv.index("--") + 1 :]


def test_registry_rows_are_read_through_the_ssot(registry: Path) -> None:
    # Arrange
    expected = {"spartan", "mba"}
    # Act
    names = {h.name for h in registry_hosts()}
    # Assert
    assert names == expected


def test_absolute_root_is_returned_verbatim(registry: Path) -> None:
    # Arrange
    host = "spartan"
    # Act
    root = remote_state_root(host)
    # Assert
    assert root == SPARTAN_ROOT


def test_home_relative_root_is_kept_raw(registry: Path) -> None:
    # Arrange
    host = "mba"
    # Act
    raw = registry_scitex_root(host)
    # Assert
    assert raw == "~/.scitex"


def test_home_relative_root_never_expands_locally(registry: Path) -> None:
    """The bug this module exists to prevent.

    ``~`` on a REMOTE host means the PEER's home. Expanding it here would
    silently yield the LEAD's home. ``remote_state_root`` must refuse to
    guess and return None, so the caller keeps the home-relative default
    that the remote expands correctly on its own.
    """
    # Arrange
    host = "mba"
    # Act
    root = remote_state_root(host)
    # Assert
    assert root is None


def test_unregistered_host_returns_none(registry: Path) -> None:
    # Arrange
    host = "definitely-not-registered"
    # Act
    root = remote_state_root(host)
    # Assert
    assert root is None


def test_compute_node_inherits_root_via_chain(registry: Path) -> None:
    """``spartan-bm043`` is NOT a registry row — it must inherit Spartan's.

    HPC compute nodes are ephemeral and rightly absent from the registry,
    but they share the login node's filesystem. Without via-chain
    inheritance the two-tier targets — the ones agents ACTUALLY run on —
    would be the only hosts left unpinned.
    """
    # Arrange
    peers = _peers()
    # Act
    root = resolve_peer_scitex_root("spartan-bm043", peers)
    # Assert
    assert root == SPARTAN_ROOT


def test_sac_invocation_is_registry_pinned(registry: Path) -> None:
    # Arrange
    peers = _peers()
    # Act
    argv = build_ssh_argv("spartan", ["sac", "agents", "start", "a"], peers)
    # Assert
    assert _remote_cmd(argv) == [
        f"SCITEX_DIR={SPARTAN_ROOT}",
        "sac",
        "agents",
        "start",
        "a",
    ]


def test_pin_survives_the_lmod_preamble_wrapper(registry: Path) -> None:
    """The compute-node path wraps in ``bash -c '<preamble> && <cmd>'``.

    The env pin must land INSIDE the wrapper, after the module loads —
    otherwise the preamble branch silently drops it and the two-tier HPC
    targets stay unpinned.
    """
    # Arrange
    peers = _peers()
    # Act
    argv = build_ssh_argv("spartan-bm043", ["sac", "agents", "start", "a"], peers)
    # Assert
    assert f"SCITEX_DIR={SPARTAN_ROOT} sac agents start a" in argv[-1]


def test_home_rooted_peer_argv_is_byte_identical(registry: Path) -> None:
    """No regression for mba / nas: their registry root IS ``~/.scitex``."""
    # Arrange
    peers = _peers()
    # Act
    argv = build_ssh_argv("mba", ["sac", "agents", "start", "a"], peers)
    # Assert
    assert _remote_cmd(argv) == ["sac", "agents", "start", "a"]


def test_non_sac_command_is_never_touched(registry: Path) -> None:
    """``sac host exec <peer> -- <arbitrary>`` must pass through unchanged."""
    # Arrange
    peers = _peers()
    # Act
    argv = build_ssh_argv("spartan", ["hostname", "-s"], peers)
    # Assert
    assert _remote_cmd(argv) == ["hostname", "-s"]


def test_absolute_path_to_sac_is_still_detected(registry: Path) -> None:
    """Spartan invokes ``/home/ywatanabe/.env-3.11/bin/sac`` — still a sac run."""
    # Arrange
    peers = _peers()
    # Act
    argv = build_ssh_argv("spartan", ["/home/x/.env-3.11/bin/sac", "agents"], peers)
    # Assert
    assert _remote_cmd(argv)[0] == f"SCITEX_DIR={SPARTAN_ROOT}"


def test_peer_absent_from_registry_is_never_pinned(
    registry_without_spartan: Path,
) -> None:
    """A peer the registry does not know must keep the pre-registry argv.

    sac must never INVENT a root for a host the SSOT has no opinion on —
    it has no better answer than it had before, so the remote keeps
    expanding its own ``~/.scitex``. Guessing here is precisely the class
    of bug this whole change exists to kill.
    """
    # Arrange
    peers = _peers()
    # Act
    argv = build_ssh_argv("spartan", ["sac", "agents", "start", "a"], peers)
    # Assert
    assert _remote_cmd(argv) == ["sac", "agents", "start", "a"]


# EOF
