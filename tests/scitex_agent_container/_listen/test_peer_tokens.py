"""Tests for ``_listen.peer_tokens`` — the WI-4 Q4(b) per-host
bearer registry that the cross-host forwarder consults.

Per HANDOFF_AGENT_COMMS_2026-05-19.md §4 (WI-4) and the lead's
2026-05-21 Q4 directive (Option (b) per-host bearer registry).

No mocks (handoff §0): real filesystem under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._listen.peer_tokens import (
    PeerTokenError,
    default_peer_tokens_dir,
    list_peer_hosts,
    read_peer_token,
    write_peer_token,
)


@pytest.fixture
def peer_tokens_dir(tmp_path: Path) -> Path:
    # Arrange
    d = tmp_path / "peer-tokens"
    return d


# ---------------------------------------------------------------------------
# default_peer_tokens_dir — path resolution
# ---------------------------------------------------------------------------


def test_default_peer_tokens_dir_lands_under_scitex_dir(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path
    # Act
    p = default_peer_tokens_dir(home=home)
    # Assert
    assert p == tmp_path / ".scitex" / "agent-container" / "peer-tokens"


# ---------------------------------------------------------------------------
# write_peer_token — atomic, mode 0600, returns the destination
# ---------------------------------------------------------------------------


def test_write_peer_token_creates_file(peer_tokens_dir: Path) -> None:
    # Arrange
    # Act
    dst = write_peer_token(
        peer_host="host-a", token="secret-A", tokens_dir=peer_tokens_dir
    )
    # Assert
    assert dst.is_file() and dst.read_text() == "secret-A"


def test_write_peer_token_sets_mode_0600(peer_tokens_dir: Path) -> None:
    # Arrange
    dst = write_peer_token(
        peer_host="host-a", token="secret-A", tokens_dir=peer_tokens_dir
    )
    # Act
    mode = dst.stat().st_mode & 0o777
    # Assert
    assert mode == 0o600


def test_write_peer_token_overwrites_existing(peer_tokens_dir: Path) -> None:
    """Re-adding a peer (token rotation) overwrites in place."""
    # Arrange
    write_peer_token(
        peer_host="host-a", token="old", tokens_dir=peer_tokens_dir
    )
    # Act
    write_peer_token(
        peer_host="host-a", token="new", tokens_dir=peer_tokens_dir
    )
    # Assert
    assert read_peer_token(peer_host="host-a", tokens_dir=peer_tokens_dir) == "new"


def test_write_peer_token_rejects_empty_host(peer_tokens_dir: Path) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        write_peer_token(peer_host="", token="t", tokens_dir=peer_tokens_dir)


def test_write_peer_token_rejects_empty_token(peer_tokens_dir: Path) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        write_peer_token(peer_host="host-a", token="", tokens_dir=peer_tokens_dir)


# ---------------------------------------------------------------------------
# read_peer_token — loud failure when the file is missing
# ---------------------------------------------------------------------------


def test_read_peer_token_returns_minted_value(peer_tokens_dir: Path) -> None:
    # Arrange
    write_peer_token(
        peer_host="host-a", token="secret-A", tokens_dir=peer_tokens_dir
    )
    # Act
    got = read_peer_token(peer_host="host-a", tokens_dir=peer_tokens_dir)
    # Assert
    assert got == "secret-A"


def test_read_peer_token_raises_loudly_when_file_missing(
    peer_tokens_dir: Path,
) -> None:
    """Missing token must fail loudly with an actionable message —
    no silent ``None`` fallback (handoff §0 Hard rules)."""
    # Arrange
    # Act
    # Assert
    with pytest.raises(PeerTokenError, match="sac host add-peer"):
        read_peer_token(peer_host="host-z", tokens_dir=peer_tokens_dir)


def test_read_peer_token_error_names_the_missing_host(
    peer_tokens_dir: Path,
) -> None:
    """The error message names the specific host so the operator
    sees which peer needs an ``add-peer`` call."""
    # Arrange
    # Act
    try:
        read_peer_token(peer_host="head-spartan", tokens_dir=peer_tokens_dir)
        msg = ""
    except PeerTokenError as exc:
        msg = str(exc)
    # Assert
    assert "head-spartan" in msg


def test_read_peer_token_raises_on_empty_file(peer_tokens_dir: Path) -> None:
    """A zero-byte token file is treated as missing (the operator
    wrote it then mis-edited it; loud failure surfaces the misuse).
    """
    # Arrange
    peer_tokens_dir.mkdir(parents=True, exist_ok=True)
    (peer_tokens_dir / "host-a.token").write_text("")
    # Act
    # Assert
    with pytest.raises(PeerTokenError, match="empty"):
        read_peer_token(peer_host="host-a", tokens_dir=peer_tokens_dir)


# ---------------------------------------------------------------------------
# list_peer_hosts — observability surface
# ---------------------------------------------------------------------------


def test_list_peer_hosts_empty_dir_returns_empty_list(
    peer_tokens_dir: Path,
) -> None:
    # Arrange
    # Act
    hosts = list_peer_hosts(tokens_dir=peer_tokens_dir)
    # Assert
    assert hosts == []


def test_list_peer_hosts_returns_sorted_names(peer_tokens_dir: Path) -> None:
    """Names only — token values are never returned."""
    # Arrange
    write_peer_token(
        peer_host="host-b", token="t-b", tokens_dir=peer_tokens_dir
    )
    write_peer_token(
        peer_host="host-a", token="t-a", tokens_dir=peer_tokens_dir
    )
    # Act
    hosts = list_peer_hosts(tokens_dir=peer_tokens_dir)
    # Assert
    assert hosts == ["host-a", "host-b"]
