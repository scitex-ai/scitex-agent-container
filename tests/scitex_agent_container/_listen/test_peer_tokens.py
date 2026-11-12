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
    write_peer_token(peer_host="host-a", token="old", tokens_dir=peer_tokens_dir)
    # Act
    write_peer_token(peer_host="host-a", token="new", tokens_dir=peer_tokens_dir)
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
    write_peer_token(peer_host="host-a", token="secret-A", tokens_dir=peer_tokens_dir)
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
    write_peer_token(peer_host="host-b", token="t-b", tokens_dir=peer_tokens_dir)
    write_peer_token(peer_host="host-a", token="t-a", tokens_dir=peer_tokens_dir)
    # Act
    hosts = list_peer_hosts(tokens_dir=peer_tokens_dir)
    # Assert
    assert hosts == ["host-a", "host-b"]


@pytest.fixture(
    params=["../outside", "..\\outside", "absolute", ".", "..", "host\nname"]
)
def unsafe_peer_name(peer_tokens_dir, request):
    """Even a broken implementation can only reach owned fixture files."""
    peer_tokens_dir.mkdir()
    (peer_tokens_dir.parent / "outside.token").write_text("outside-fixture")
    if request.param == "absolute":
        return str(peer_tokens_dir.parent / "outside")
    return request.param


def test_peer_reads_cannot_supply_paths(peer_tokens_dir, unsafe_peer_name):
    # Arrange
    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        read_peer_token(peer_host=unsafe_peer_name, tokens_dir=peer_tokens_dir)


def test_peer_writes_cannot_supply_paths(peer_tokens_dir, unsafe_peer_name):
    # Arrange
    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        write_peer_token(
            peer_host=unsafe_peer_name,
            token="fixture-token",
            tokens_dir=peer_tokens_dir,
        )


@pytest.fixture
def token_symlink_root(tmp_path):
    root = tmp_path / "tokens"
    root.mkdir()
    outside = tmp_path / "outside.token"
    outside.write_text("outside-fixture")
    (root / "host-a.token").symlink_to(outside)
    return root


def test_token_reads_reject_symlink_credential_entries(token_symlink_root):
    # Arrange
    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        read_peer_token(peer_host="host-a", tokens_dir=token_symlink_root)


def test_token_writes_reject_symlink_credential_entries(token_symlink_root):
    # Arrange
    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        write_peer_token(
            peer_host="host-a", token="fixture-token", tokens_dir=token_symlink_root
        )


def test_token_staging_does_not_follow_a_predictable_temp_symlink(tmp_path):
    # Arrange
    root = tmp_path / "tokens"
    root.mkdir()
    outside = tmp_path / "outside.token"
    outside.write_text("outside-fixture")
    (root / "host-a.token.tmp").symlink_to(outside)
    # Act
    destination = write_peer_token(
        peer_host="host-a", token="new-fixture", tokens_dir=root
    )
    # Assert
    assert (destination.read_text(), outside.read_text()) == (
        "new-fixture",
        "outside-fixture",
    )


def test_list_does_not_advertise_a_symlink_as_a_peer_token(tmp_path):
    # Arrange
    root = tmp_path / "tokens"
    root.mkdir()
    outside = tmp_path / "outside.token"
    outside.write_text("outside-fixture")
    (root / "host-a.token").symlink_to(outside)
    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        list_peer_hosts(root)


def test_configured_registry_directory_may_be_a_symlink(tmp_path):
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    # Act
    write_peer_token(peer_host="host.example", token="fixture-token", tokens_dir=alias)
    result = read_peer_token(peer_host="host.example", tokens_dir=alias)
    # Assert
    assert result == "fixture-token"


@pytest.mark.parametrize("name", ["C:outside", "C:\\outside", "\\\\server\\share"])
def test_path_resolution_rejects_windows_drives_without_touching_files(
    peer_tokens_dir, name
):
    # Arrange
    from scitex_agent_container._listen.peer_tokens import peer_token_path

    # Act
    # Assert
    with pytest.raises(PeerTokenError):
        peer_token_path(name, peer_tokens_dir)


@pytest.mark.parametrize(
    "name", ["host-a", "host.example", "192.0.2.4", "::1", "a::1", "C::1"]
)
def test_safe_host_aliases_and_ip_literals_keep_their_identity(peer_tokens_dir, name):
    # Arrange
    # Act
    destination = write_peer_token(
        peer_host=name, token="fixture-token", tokens_dir=peer_tokens_dir
    )
    value = read_peer_token(peer_host=name, tokens_dir=peer_tokens_dir)
    # Assert
    assert (destination.name, value) == (name + ".token", "fixture-token")


def _missing_peer_hint(peer_host, directory):
    try:
        read_peer_token(peer_host=peer_host, tokens_dir=directory)
    except PeerTokenError as error:
        return str(error).partition("Add one with: ")[2]
    return ""


@pytest.mark.parametrize(
    "peer_host",
    ["peer name", "peer;touch owned-marker", "$(touch owned-marker)", "--peer-option"],
)
def test_missing_peer_remediation_preserves_one_literal_host(
    peer_tokens_dir, peer_host
):
    # Arrange
    import shlex

    expected = ["sac", "host", "add-peer", "--yes", "--", peer_host, "TOKEN"]
    # Act
    hint = _missing_peer_hint(peer_host, peer_tokens_dir)
    parsed = shlex.split(hint)
    # Assert
    assert parsed == expected
