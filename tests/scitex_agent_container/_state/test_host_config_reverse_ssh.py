"""Tests for the ``reverse_ssh`` peer field (ADR-0021 push-config route).

Focused sibling of ``test_host_config.py`` (same pattern as the
``resolve`` / ``env_preamble`` / ``lead`` siblings). PA-306: no mocks —
real YAML files loaded through the real ``load()``. Each test: AAA
markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.host_config import PeerSpec, load


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


def test_load_parses_reverse_ssh_from_yaml(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        "peers:\n  nas:\n    ssh: admin@192.168.11.22\n    reverse_ssh: master-x\n"
    )
    # Act
    cfg = load(cfg_path)
    # Assert
    assert cfg.peers["nas"].reverse_ssh == "master-x"


def test_reverse_ssh_defaults_to_empty_when_absent(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: {ssh: m}\n")
    # Act
    cfg = load(cfg_path)
    # Assert
    assert cfg.peers["mba"].reverse_ssh == ""


def test_from_dict_parses_reverse_ssh_field():
    # Arrange
    spec = {"ssh": "sp", "reverse_ssh": "ywata-note-win"}
    # Act
    peer = PeerSpec.from_dict(spec, name="spartan")
    # Assert
    assert peer.reverse_ssh == "ywata-note-win"


def test_from_dict_defaults_reverse_ssh_to_empty():
    # Arrange
    spec = {"ssh": "sp"}
    # Act
    peer = PeerSpec.from_dict(spec, name="spartan")
    # Assert
    assert peer.reverse_ssh == ""


def test_validate_accepts_a_peer_with_reverse_ssh(cfg_path: Path):
    # Arrange
    # nas-03, not nas: the bare name is a moving alias and validate() now
    # refuses it, so a fixture using it would be asserting on the wrong thing.
    cfg_path.write_text("peers:\n  nas-03:\n    ssh: n\n    reverse_ssh: master-x\n")
    cfg = load(cfg_path)
    # Act
    errors = cfg.validate()
    # Assert
    assert errors == []
