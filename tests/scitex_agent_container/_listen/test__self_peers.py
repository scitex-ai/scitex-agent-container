"""Tests for the generic self-peer discovery shape (op-2026-06-12-15).

The self-peer convention is GENERIC — no name-specific hacks. The
literal ``self`` directory means "register the running session
under its RUNTIME identity"; the name comes from a *self_identity*
argument the listen passes in at scan time. Any other directory
name is taken verbatim as the peer's name. No code-level
discriminators for ``lead`` / ``operator`` / any other identifier.

Test style (STX-TQ002 / TQ007): explicit ``# Arrange`` / ``# Act`` /
``# Assert`` markers in order; one assertion per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._listen._self_peers import (
    discover_self_peers,
    is_self_peer_spec,
    load_self_peer,
)

# ---------------------------------------------------------------------------
# is_self_peer_spec — pure predicate
# ---------------------------------------------------------------------------


def test_is_self_peer_spec_accepts_minimal_listen_only_mapping():
    # Arrange
    blob = {"listen_url": "http://127.0.0.1:7878"}
    # Act
    accepted = is_self_peer_spec(blob)
    # Assert
    assert accepted is True


def test_is_self_peer_spec_rejects_blob_without_listen_url():
    # Arrange
    blob = {"description": "no listen url here"}
    # Act
    accepted = is_self_peer_spec(blob)
    # Assert
    assert accepted is False


def test_is_self_peer_spec_rejects_blob_with_spec_key_container_marker():
    # Arrange
    blob = {
        "listen_url": "http://127.0.0.1:7878",
        "spec": {"runtime": "apptainer"},
    }
    # Act
    accepted = is_self_peer_spec(blob)
    # Assert
    assert accepted is False


def test_is_self_peer_spec_rejects_blob_with_apiversion_container_marker():
    # Arrange
    blob = {
        "listen_url": "http://127.0.0.1:7878",
        "apiVersion": "scitex-agent-container/v3",
    }
    # Act
    accepted = is_self_peer_spec(blob)
    # Assert
    assert accepted is False


def test_is_self_peer_spec_rejects_non_mapping_input_none():
    # Arrange / Act
    accepted = is_self_peer_spec(None)
    # Assert
    assert accepted is False


# ---------------------------------------------------------------------------
# load_self_peer — read one file
# ---------------------------------------------------------------------------


def _write_spec(dir_: Path, name: str, body: str) -> Path:
    """Helper — write ``<dir>/<name>/spec.yaml`` and return the path."""
    sub = dir_ / name
    sub.mkdir(parents=True, exist_ok=True)
    path = sub / "spec.yaml"
    path.write_text(body)
    return path


def test_load_self_peer_uses_self_identity_for_literal_self_dir(
    tmp_path: Path,
):
    # Arrange: the operator-canonical shape — literal ``self/`` dir,
    # listen-only spec, runtime identity supplied by the caller.
    spec = _write_spec(tmp_path, "self", "listen_url: http://127.0.0.1:7878\n")
    # Act
    peer = load_self_peer(spec, self_identity="capsule-7")
    # Assert
    assert peer is not None and peer["name"] == "capsule-7"


def test_load_self_peer_falls_back_to_dir_name_when_dir_is_not_self(
    tmp_path: Path,
):
    # Arrange: non-``self`` dir → name comes verbatim from dir.
    spec = _write_spec(tmp_path, "beta-pointer", "listen_url: http://127.0.0.1:9001\n")
    # Act
    peer = load_self_peer(spec, self_identity="should-be-ignored")
    # Assert
    assert peer is not None and peer["name"] == "beta-pointer"


def test_load_self_peer_degrades_self_dir_without_identity_to_literal_self(
    tmp_path: Path,
):
    # Arrange: literal ``self/`` but caller forgot the identity —
    # the loader degrades gracefully to ``"self"`` rather than raising.
    spec = _write_spec(tmp_path, "self", "listen_url: http://127.0.0.1:7878\n")
    # Act
    peer = load_self_peer(spec)
    # Assert
    assert peer is not None and peer["name"] == "self"


def test_load_self_peer_marks_kind_field_as_self_peer(tmp_path: Path):
    # Arrange
    spec = _write_spec(tmp_path, "gamma", "listen_url: http://127.0.0.1:9002\n")
    # Act
    peer = load_self_peer(spec)
    # Assert
    assert peer is not None and peer["kind"] == "self-peer"


def test_load_self_peer_returns_none_for_container_spec(tmp_path: Path):
    # Arrange
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n  runtime: apptainer\n"
    )
    spec = _write_spec(tmp_path, "container-agent", body)
    # Act
    peer = load_self_peer(spec)
    # Assert
    assert peer is None


def test_load_self_peer_returns_none_for_malformed_yaml(tmp_path: Path):
    # Arrange
    spec = _write_spec(tmp_path, "broken", "not: [valid: yaml:\n")
    # Act
    peer = load_self_peer(spec)
    # Assert
    assert peer is None


# ---------------------------------------------------------------------------
# discover_self_peers — walk multiple dirs
# ---------------------------------------------------------------------------


def test_discover_self_peers_resolves_literal_self_dir_via_self_identity(
    tmp_path: Path,
):
    # Arrange
    base = tmp_path / "base"
    _write_spec(base, "self", "listen_url: http://127.0.0.1:7878\n")
    # Act
    peers = discover_self_peers([base], self_identity="capsule-42")
    # Assert: dir literal stays generic; name comes from runtime identity.
    assert [p["name"] for p in peers] == ["capsule-42"]


def test_discover_self_peers_returns_sorted_unique_name_list(tmp_path: Path):
    # Arrange: two dirs, same dir name; higher-priority wins.
    high = tmp_path / "high"
    low = tmp_path / "low"
    _write_spec(high, "alpha", "listen_url: http://h:1\n")
    _write_spec(low, "alpha", "listen_url: http://l:1\n")
    _write_spec(low, "beta", "listen_url: http://l:2\n")
    # Act
    peers = discover_self_peers([high, low])
    # Assert
    assert [p["name"] for p in peers] == ["alpha", "beta"]


def test_discover_self_peers_higher_priority_dir_wins_on_duplicate_name(
    tmp_path: Path,
):
    # Arrange
    high = tmp_path / "high"
    low = tmp_path / "low"
    _write_spec(high, "alpha", "listen_url: http://winner:1\n")
    _write_spec(low, "alpha", "listen_url: http://loser:1\n")
    # Act
    peers = discover_self_peers([high, low])
    # Assert
    assert peers[0]["listen_url"] == "http://winner:1"


def test_discover_self_peers_silently_skips_nonexistent_search_dir(
    tmp_path: Path,
):
    # Arrange
    real = tmp_path / "real"
    _write_spec(real, "alpha", "listen_url: http://x:1\n")
    ghost = tmp_path / "does-not-exist"
    # Act
    peers = discover_self_peers([ghost, real])
    # Assert
    assert [p["name"] for p in peers] == ["alpha"]


def test_discover_self_peers_skips_container_agent_dirs(tmp_path: Path):
    # Arrange: a self-peer + a container agent in the same base dir.
    base = tmp_path / "base"
    _write_spec(base, "self-node", "listen_url: http://127.0.0.1:9000\n")
    _write_spec(
        base,
        "container-node",
        "apiVersion: scitex-agent-container/v3\nspec:\n  runtime: apptainer\n",
    )
    # Act
    peers = discover_self_peers([base])
    # Assert: only the self-peer is returned.
    assert [p["name"] for p in peers] == ["self-node"]
