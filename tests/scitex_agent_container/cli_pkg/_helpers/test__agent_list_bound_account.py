"""Tests for the Account column's LIVE credential-bind resolver.

Fixtures are REAL ``mountinfo`` lines captured from a running agent container
on 2026-08-02, not hand-invented shapes — the previous Account resolver was
wrong precisely because nobody checked it against what the system actually
holds, and a hand-written fixture would have reproduced my assumption rather
than the machine's answer.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._helpers._agent_list_bound_account import (
    account_from_mountinfo,
    bound_account_for,
)

# Captured verbatim from /proc/<pid>/mountinfo of a live container.
REAL_BIND_LINE = (
    "2749 2730 8:48 "
    "/home/ywatanabe/.dotfiles/src/.scitex/agent-container/accounts/anthropic/"
    "wyusuuke-gmail-com/.credentials.json "
    "/home/agent/.claude/.credentials.json rw,nosuid,nodev,relatime master:797 "
    "- ext4 /dev/sdd rw,discard,errors=remount-ro,data=ordered"
)

# A same-container line that is NOT the credential bind.
UNRELATED_LINE = (
    "2748 2730 8:48 /home/ywatanabe/proj/scitex-agent-container /work "
    "rw,nosuid,nodev,relatime master:797 - ext4 /dev/sdd rw,discard"
)


def test_extracts_account_from_real_bind_line() -> None:
    # Arrange
    mountinfo = f"{UNRELATED_LINE}\n{REAL_BIND_LINE}\n"
    # Act
    account = account_from_mountinfo(mountinfo)
    # Assert
    assert account == "wyusuuke-gmail-com"


def test_ignores_mount_table_without_credential_bind() -> None:
    # Arrange — a table holding only non-credential mounts.
    mountinfo = f"{UNRELATED_LINE}\n"
    # Act
    account = account_from_mountinfo(mountinfo)
    # Assert
    assert account == ""


def test_does_not_read_an_accounts_path_bound_elsewhere() -> None:
    # Arrange — the SOURCE looks like an account credentials file but it is
    # NOT mounted onto the container credentials path. Reading it would
    # report an account the agent does not authenticate with.
    decoy = REAL_BIND_LINE.replace(
        "/home/agent/.claude/.credentials.json", "/home/agent/backup/creds.json"
    )
    # Act
    account = account_from_mountinfo(decoy)
    # Assert
    assert account == ""


def test_layout_without_provider_segment_still_resolves() -> None:
    # Arrange — the older accounts/<name>/ layout, with no provider segment.
    legacy = REAL_BIND_LINE.replace("accounts/anthropic/", "accounts/")
    # Act
    account = account_from_mountinfo(legacy)
    # Assert
    assert account == "wyusuuke-gmail-com"


def test_empty_mount_table_resolves_to_nothing() -> None:
    # Arrange
    mountinfo = ""
    # Act
    account = account_from_mountinfo(mountinfo)
    # Assert
    assert account == ""


def test_unknown_agent_resolves_to_none_not_a_guess() -> None:
    # Arrange — a name no live container claims. The caller must fall back to
    # its weaker signals, so this MUST be None rather than a stand-in string.
    name = "no-such-agent-nobody-is-running-this"
    # Act
    resolved = bound_account_for(name)
    # Assert
    assert resolved is None
