"""Tests for the optional ``resolve:`` field on peer entries (Phase 1).

Phase 1 of the dispatch-time-node-resolution architecture (full plan
at ``~/proj/scitex-lead/GITIGNORED/FUTURE/sac-dispatch-time-node-resolution.md``).
Phase 1 scope: schema + parser only — no resolver code path runs. The
sister file ``test_host_config.py`` covers the pre-existing peer
schema; this file pins:

- ``PeerSpec.from_dict`` accepts a ``resolve:`` mapping and rejects
  malformed shapes with typed errors (``ValueError``).
- Phase 1 accepts only ``source: scitex-hpc``; other sources raise.
- A peer with both ``resolve`` and ``ssh`` retains both (the explicit
  ssh is preserved verbatim as a static fallback).
- A peer without ``resolve`` parses with ``resolve=None`` so the
  default code path is byte-identical to the pre-resolve schema.
- The full ``load()``-from-YAML path round-trips a Spartan-style
  reservation label.
- ``Config.validate()`` lets ``ssh: ""`` slide when ``resolve:`` is
  set, but still flags peers that have neither.
- Pre-existing peer shapes (mba, spartan, multi-hop, env_preamble)
  still parse unchanged through ``from_dict``.

No-mocks pattern (PA-306) — env mutations route through the shared
``env_save_restore`` fixture; ``load()`` reads a real on-disk file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import (
    Config,
    HostBlock,
    PeerSpec,
    ResolveSpec,
    load,
)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# PeerSpec.from_dict — happy paths
# ---------------------------------------------------------------------------


def test_peer_spec_resolve_defaults_to_none_when_field_absent():
    # Arrange
    peer = PeerSpec.from_dict({"ssh": "user@mba.local"}, name="mba")
    # Act
    out = peer.resolve
    # Assert
    assert out is None


def test_peer_spec_from_dict_parses_resolve_source_and_reservation():
    # Arrange
    spec = {
        "via": ["spartan"],
        "resolve": {
            "source": "scitex-hpc",
            "reservation": "spartan-cpu-64-ram-256",
        },
    }
    # Act
    peer = PeerSpec.from_dict(spec, name="spartan-cpu-reservation")
    # Assert
    assert peer.resolve == ResolveSpec(
        source="scitex-hpc",
        reservation="spartan-cpu-64-ram-256",
    )


def test_peer_spec_from_dict_allows_resolve_without_reservation():
    # Arrange — Phase 1 schema does not yet enforce per-source
    # required keys; reservation is optional at the parse layer.
    spec = {"resolve": {"source": "scitex-hpc"}}
    # Act
    peer = PeerSpec.from_dict(spec, name="future-label")
    # Assert
    assert peer.resolve == ResolveSpec(source="scitex-hpc", reservation=None)


def test_peer_spec_from_dict_keeps_resolve_when_static_ssh_also_set():
    # Arrange — explicit ssh: alongside resolve: is OK; both are kept
    # so Phase 2 can decide the precedence rule.
    spec = {
        "ssh": "ywatanabe@spartan-bm152",
        "resolve": {
            "source": "scitex-hpc",
            "reservation": "spartan-cpu-64-ram-256",
        },
    }
    # Act
    peer = PeerSpec.from_dict(spec, name="spartan-cpu-reservation")
    # Assert
    assert peer.resolve is not None


def test_peer_spec_from_dict_keeps_static_ssh_when_resolve_also_set():
    # Arrange — pair-test of the above: the explicit ssh string must
    # survive the round-trip unchanged verbatim.
    spec = {
        "ssh": "ywatanabe@spartan-bm152",
        "resolve": {
            "source": "scitex-hpc",
            "reservation": "spartan-cpu-64-ram-256",
        },
    }
    # Act
    peer = PeerSpec.from_dict(spec, name="spartan-cpu-reservation")
    # Assert
    assert peer.ssh == "ywatanabe@spartan-bm152"


# ---------------------------------------------------------------------------
# PeerSpec.from_dict — malformed shapes raise ValueError
# ---------------------------------------------------------------------------


def test_peer_spec_from_dict_rejects_resolve_missing_source():
    # Arrange
    spec = {"resolve": {"reservation": "spartan-cpu-64-ram-256"}}
    # Act
    raised = pytest.raises(ValueError, match="resolve.source")
    # Assert
    with raised:
        PeerSpec.from_dict(spec, name="bad-no-source")


def test_peer_spec_from_dict_rejects_unknown_resolve_source():
    # Arrange
    spec = {"resolve": {"source": "kubernetes", "reservation": "x"}}
    # Act
    raised = pytest.raises(ValueError, match="kubernetes")
    # Assert
    with raised:
        PeerSpec.from_dict(spec, name="bad-source")


def test_peer_spec_from_dict_rejects_non_dict_resolve_block():
    # Arrange — scalar / list / etc. all surface as a typed error.
    spec = {"resolve": "scitex-hpc"}
    # Act
    raised = pytest.raises(ValueError, match="resolve")
    # Assert
    with raised:
        PeerSpec.from_dict(spec, name="bad-scalar")


def test_peer_spec_from_dict_rejects_non_string_reservation():
    # Arrange
    spec = {"resolve": {"source": "scitex-hpc", "reservation": 42}}
    # Act
    raised = pytest.raises(ValueError, match="reservation")
    # Assert
    with raised:
        PeerSpec.from_dict(spec, name="bad-reservation")


def test_peer_spec_from_dict_rejects_empty_string_source():
    # Arrange
    spec = {"resolve": {"source": "   "}}
    # Act
    raised = pytest.raises(ValueError, match="resolve.source")
    # Assert
    with raised:
        PeerSpec.from_dict(spec, name="bad-blank")


# ---------------------------------------------------------------------------
# PeerSpec.from_dict — pre-existing peer shapes still parse unchanged
# ---------------------------------------------------------------------------


def test_peer_spec_from_dict_parses_mba_style_peer_unchanged():
    # Arrange
    peer = PeerSpec.from_dict({"ssh": "ywatanabe@mba.local"}, name="mba")
    # Act
    expected = PeerSpec(name="mba", ssh="ywatanabe@mba.local")
    # Assert
    assert peer == expected


def test_peer_spec_from_dict_parses_nas_style_peer_unchanged():
    # Arrange
    peer = PeerSpec.from_dict({"ssh": "admin@192.168.11.22"}, name="nas")
    # Act
    expected = PeerSpec(name="nas", ssh="admin@192.168.11.22")
    # Assert
    assert peer == expected


def test_peer_spec_from_dict_parses_ywata_note_win_style_peer_unchanged():
    # Arrange
    peer = PeerSpec.from_dict(
        {"ssh": "ywatanabe@ywata-note-win"}, name="ywata-note-win"
    )
    # Act
    expected = PeerSpec(name="ywata-note-win", ssh="ywatanabe@ywata-note-win")
    # Assert
    assert peer == expected


@pytest.fixture
def multihop_static_spartan_peer() -> PeerSpec:
    """Parse a pre-resolve static bm152-style peer with a via chain.

    Shared by the quartet of single-assert tests below that each pin
    one facet of the parse (no resolve, ssh, via, env_preamble) so a
    regression surfaces against exactly the wrong field.
    """
    spec = {
        "ssh": "spartan-bm152",
        "via": ["mba", "spartan"],
        "env_preamble": "module load Apptainer/1.3.3\n",
    }
    return PeerSpec.from_dict(spec, name="spartan-bm152")


def test_peer_spec_from_dict_static_peer_resolve_is_none(
    multihop_static_spartan_peer: PeerSpec,
):
    # Arrange: fixture builds the parsed peer.
    peer = multihop_static_spartan_peer
    # Act
    out = peer.resolve
    # Assert
    assert out is None


def test_peer_spec_from_dict_static_peer_preserves_ssh(
    multihop_static_spartan_peer: PeerSpec,
):
    # Arrange: fixture builds the parsed peer.
    peer = multihop_static_spartan_peer
    # Act
    out = peer.ssh
    # Assert
    assert out == "spartan-bm152"


def test_peer_spec_from_dict_static_peer_preserves_via_chain(
    multihop_static_spartan_peer: PeerSpec,
):
    # Arrange: fixture builds the parsed peer.
    peer = multihop_static_spartan_peer
    # Act
    out = peer.via
    # Assert
    assert out == ("mba", "spartan")


def test_peer_spec_from_dict_static_peer_preserves_env_preamble(
    multihop_static_spartan_peer: PeerSpec,
):
    # Arrange: fixture builds the parsed peer.
    peer = multihop_static_spartan_peer
    # Act
    out = peer.env_preamble
    # Assert
    assert out == ("module load Apptainer/1.3.3",)


# ---------------------------------------------------------------------------
# Full load() round-trip via YAML
# ---------------------------------------------------------------------------


def test_load_parses_reservation_label_peer_from_yaml(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan-cpu-reservation:
    via: [spartan]
    env_preamble: |
      source /usr/share/lmod/lmod/init/bash
      module load GCCcore/11.3.0
      module load Apptainer/1.3.3
    resolve:
      source: scitex-hpc
      reservation: spartan-cpu-64-ram-256
"""
    )
    # Act
    cfg = load()
    peer = cfg.peers["spartan-cpu-reservation"]
    # Assert
    assert peer.resolve == ResolveSpec(
        source="scitex-hpc",
        reservation="spartan-cpu-64-ram-256",
    )


def test_load_reservation_label_peer_via_chain_is_preserved(cfg_path: Path):
    # Arrange — pair-test of the above: the via: chain must coexist
    # with resolve: (the resolver shells out via[-1]).
    cfg_path.write_text(
        """
peers:
  spartan-cpu-reservation:
    via: [spartan]
    resolve:
      source: scitex-hpc
      reservation: spartan-cpu-64-ram-256
"""
    )
    # Act
    cfg = load()
    # Assert
    assert cfg.peers["spartan-cpu-reservation"].via == ("spartan",)


def test_load_rejects_unknown_resolve_source_in_yaml(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  bogus:
    via: [spartan]
    resolve:
      source: kubernetes
      reservation: foo
"""
    )
    # Act
    raised = pytest.raises(ValueError, match="kubernetes")
    # Assert
    with raised:
        load()


def test_load_rejects_resolve_missing_source_in_yaml(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  bogus:
    via: [spartan]
    resolve:
      reservation: foo
"""
    )
    # Act
    raised = pytest.raises(ValueError, match="resolve.source")
    # Assert
    with raised:
        load()


# ---------------------------------------------------------------------------
# Config.validate — resolve relaxes the ssh-required rule
# ---------------------------------------------------------------------------


def test_validate_accepts_empty_ssh_when_resolve_is_set():
    # Arrange — Phase 1 lets resolve: stand in for ssh: at validate time
    # so peers.yaml can declare label-only peers ahead of Phase 2.
    cfg = Config(
        host=HostBlock(),
        peers={
            "spartan": PeerSpec(name="spartan", ssh="spartan"),
            "spartan-cpu-reservation": PeerSpec(
                name="spartan-cpu-reservation",
                ssh="",
                via=("spartan",),
                resolve=ResolveSpec(
                    source="scitex-hpc",
                    reservation="spartan-cpu-64-ram-256",
                ),
            ),
        },
    )
    # Act
    errors = cfg.validate()
    # Assert
    assert errors == []


def test_validate_still_flags_peer_with_neither_ssh_nor_resolve():
    # Arrange
    cfg = Config(
        host=HostBlock(),
        peers={
            "broken": PeerSpec(name="broken", ssh=""),
        },
    )
    # Act
    errors = cfg.validate()
    # Assert
    assert any("ssh" in err for err in errors)
