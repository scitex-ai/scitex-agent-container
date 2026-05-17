"""Tests for ``PeersMap`` glob-pattern peer lookup.

Carved into its own file (rather than appended to
``test_host_config.py``) because the parent test module is already at
the project's per-file line ceiling. Sister-test file convention
matches the existing ``test_host_config_env_preamble.py`` /
``test_host_config_resolve.py`` siblings under this dir.

Covers:
- Exact-key lookup wins over a glob pattern (both ``spartan-bm043`` and
  ``spartan-*`` present → literal entry returned verbatim).
- Glob-only lookup synthesizes a :class:`PeerSpec` whose ``name`` is the
  *queried* hostname, ``ssh`` falls back to the hostname when the pattern
  left it blank (but explicit ssh is preserved), and ``via`` /
  ``env_preamble`` are inherited from the pattern entry.
- ``__contains__`` / ``get`` / ``__getitem__`` glob-fallback semantics.
- Iteration enumerates the literal pattern keys (not expanded matches).
- ``load`` accepts pattern entries: ``validate()`` does not raise the
  "ssh required" error on a pattern key with no explicit ssh, and
  ``cfg.peer('<matched-hostname>')`` returns the synthesized peer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import (
    PeersMap,
    PeerSpec,
    load,
)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# PeersMap.__getitem__ / __contains__ / get — pure unit-level semantics.
# ---------------------------------------------------------------------------


def test_peers_map_exact_lookup_wins_over_glob():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(
        name="spartan-*", ssh="", via=("spartan",), env_preamble=("module load X",)
    )
    peers["spartan-bm043"] = PeerSpec(name="spartan-bm043", ssh="explicit-target")
    # Act
    got = peers["spartan-bm043"]
    # Assert
    assert got.ssh == "explicit-target"


def test_peers_map_glob_matches_when_no_exact():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    # Act
    got = peers["spartan-bm043"]
    # Assert
    assert isinstance(got, PeerSpec)


def test_peers_map_glob_match_preserves_env_preamble():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(
        name="spartan-*",
        ssh="",
        via=("spartan",),
        env_preamble=("module load GCCcore/11.3.0", "module load Apptainer/1.3.3"),
    )
    # Act
    got = peers["spartan-bm043"]
    # Assert
    assert got.env_preamble == (
        "module load GCCcore/11.3.0",
        "module load Apptainer/1.3.3",
    )


def test_peers_map_glob_match_preserves_via_chain():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("mba", "spartan"))
    # Act
    got = peers["spartan-bm043"]
    # Assert
    assert got.via == ("mba", "spartan")


def test_peers_map_glob_match_sets_synthesized_name_to_query():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    # Act
    got = peers["spartan-bm043"]
    # Assert
    assert got.name == "spartan-bm043"


def test_peers_map_glob_match_synthesizes_ssh_to_hostname():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    # Act
    got = peers["spartan-bm043"]
    # Assert
    assert got.ssh == "spartan-bm043"


def test_peers_map_glob_match_keeps_explicit_ssh_when_set():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="some-target", via=("spartan",))
    # Act
    got = peers["spartan-bm043"]
    # Assert
    assert got.ssh == "some-target"


def test_peers_map_unknown_host_raises_keyerror():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    # Act
    raised = pytest.raises(KeyError)
    # Assert
    with raised:
        peers["nas"]


def test_peers_map_unknown_host_returns_default_via_get():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    sentinel = object()
    # Act
    got = peers.get("nas", default=sentinel)
    # Assert
    assert got is sentinel


def test_peers_map_contains_returns_true_for_glob_match():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    # Act
    present = "spartan-bm043" in peers
    # Assert
    assert present is True


def test_peers_map_contains_returns_false_for_no_match():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    # Act
    present = "nas" in peers
    # Assert
    assert present is False


def test_peers_map_question_mark_glob_matches_one_char():
    # Arrange
    peers = PeersMap()
    peers["node?"] = PeerSpec(name="node?", ssh="", via=())
    # Act
    matches_one = "node1" in peers
    matches_two = "node12" in peers
    # Assert
    assert (matches_one, matches_two) == (True, False)


def test_peers_map_iteration_yields_literal_pattern_keys():
    # Arrange
    peers = PeersMap()
    peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="", via=("spartan",))
    # Act
    keys = list(peers)
    # Assert
    assert keys == ["spartan-*"]


# ---------------------------------------------------------------------------
# load() + Config.validate() + Config.peer() — end-to-end via YAML fixture.
# ---------------------------------------------------------------------------


def test_load_with_glob_pattern_validates_clean(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: ywatanabe@spartan-login1
  spartan-*:
    via: [spartan]
    env_preamble:
      - module load GCCcore/11.3.0
      - module load Apptainer/1.3.3
"""
    )
    # Act
    errors = load().validate()
    # Assert
    assert errors == []


def test_load_with_glob_pattern_resolves_compute_node(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: ywatanabe@spartan-login1
  spartan-*:
    via: [spartan]
    env_preamble:
      - module load GCCcore/11.3.0
      - module load Apptainer/1.3.3
"""
    )
    # Act
    got = load().peer("spartan-bm043")
    # Assert
    assert got.env_preamble == (
        "module load GCCcore/11.3.0",
        "module load Apptainer/1.3.3",
    )
