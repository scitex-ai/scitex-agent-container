"""Writes into the account store, once that store is SHARED with the host.

Card ``sac-container-home-splits-the-account-registry-20260815``. Binding
the HOST account registry read-only into every container
(``runtimes/_p3a_default_binds.accounts_store_bind``) changes what an
in-container write MEANS: the directory being written is no longer a
private copy nobody reads.

Three write paths were written against a private directory and answer
wrongly against a shared one. Each gets tests here, and each arranges the
dangerous condition with a REAL filesystem fact rather than a patched
function:

* ``switch_account`` — a hard link gives the live credential file and a
  stored snapshot ONE inode under two names, which is exactly the property
  a ``:rw`` file-bind produces (``credentials_file_bind`` binds the host
  snapshot at ``<container_home>/.claude/.credentials.json``; measured in
  ``/proc/self/mountinfo`` on scitex-compute-04). A bind mount cannot be
  created without privileges CI does not have; the inode identity under
  test is identical either way.

* ``save_account`` — a non-writable store directory stands in for the
  read-only mount. Skipped as root, where mode bits do not bind.

* ``delete_account`` — the same, and the most important of the three:
  ``ignore_errors=True`` made "deleted it" and "could not delete it and
  did not look" the same ``True``.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state._account_store_guard import SharedStoreWriteRefused
from scitex_agent_container._state.account_store import (
    delete_account,
    save_account,
    switch_account,
)

_ROOT_SKIP = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="running as root ignores mode bits, so a read-only store cannot be staged",
)


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Path]:
    """An isolated ``$HOME`` holding both ``~/.claude`` and the store."""
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    saved = os.environ.get("SCITEX_DIR")
    os.environ.pop("SCITEX_DIR", None)
    try:
        yield h
    finally:
        if saved is not None:
            os.environ["SCITEX_DIR"] = saved


def _store(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _write_account(store: Path, name: str) -> Path:
    account_dir = store / name
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "account.json").write_text(
        json.dumps({"name": name}), encoding="utf-8"
    )
    (account_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": f"token-{name}"}}),
        encoding="utf-8",
    )
    return account_dir


@pytest.fixture
def pinned_to_account_a(home: Path) -> Path:
    """Two accounts, with the live credential HARD-LINKED to account A.

    One inode, two names — the same property a ``:rw`` credential
    file-bind produces for a pinned agent.
    """
    store = _store(home)
    account_a = _write_account(store, "account-a")
    _write_account(store, "account-b")
    os.link(account_a / ".credentials.json", home / ".claude" / ".credentials.json")
    return account_a


@pytest.fixture
def readonly_store(home: Path) -> Iterator[Path]:
    """An existing store the process cannot write into."""
    store = _store(home)
    store.mkdir(parents=True)
    store.chmod(0o500)
    try:
        yield store
    finally:
        store.chmod(0o700)


@pytest.fixture
def readonly_store_holding_account_a(home: Path) -> Iterator[Path]:
    """A read-only store that already holds ``account-a``."""
    store = _store(home)
    _write_account(store, "account-a")
    store.chmod(0o500)
    try:
        yield store
    finally:
        store.chmod(0o700)


# ---------------------------------------------------------------------------
# switch_account — the live credential IS a registry entry
# ---------------------------------------------------------------------------


def test_switch_refuses_when_the_live_credential_is_a_stored_snapshot(
    home: Path, pinned_to_account_a: Path
) -> None:
    # Arrange — agent pinned to account A (see the fixture).
    target = "account-b"
    # Act
    result = switch_account(target, home=home)
    # Assert
    assert result["success"] is False


def test_switch_refusal_names_the_account_whose_snapshot_was_at_risk(
    home: Path, pinned_to_account_a: Path
) -> None:
    # Arrange — a refusal an operator cannot act on is only a different
    # kind of silence, so the message must name the endangered account.
    target = "account-b"
    # Act
    result = switch_account(target, home=home)
    # Assert
    assert "account-a" in result["message"]


def test_switch_leaves_the_endangered_snapshot_byte_identical(
    home: Path, pinned_to_account_a: Path
) -> None:
    # Arrange — the invariant that actually matters: account A's REGISTRY
    # entry must still hold account A's credential afterwards.
    before = (pinned_to_account_a / ".credentials.json").read_text(encoding="utf-8")
    # Act
    switch_account("account-b", home=home)
    # Assert
    assert (pinned_to_account_a / ".credentials.json").read_text(
        encoding="utf-8"
    ) == before


def test_switch_still_succeeds_when_the_live_credential_is_a_private_file(
    home: Path,
) -> None:
    # Arrange — the normal case must be untouched: an ordinary ~/.claude
    # file shares no inode with anything in the store.
    store = _store(home)
    _write_account(store, "account-a")
    _write_account(store, "account-b")
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    # Act
    result = switch_account("account-b", home=home)
    # Assert
    assert result["success"] is True


def test_switch_still_succeeds_when_no_live_credential_exists_yet(home: Path) -> None:
    # Arrange — a destination that does not exist cannot alias anything,
    # and the guard must not invent a reason to refuse a first switch.
    _write_account(_store(home), "account-b")
    # Act
    result = switch_account("account-b", home=home)
    # Assert
    assert result["success"] is True


def test_switch_reports_an_unknown_account_as_before(home: Path) -> None:
    # Arrange — the guard runs after the account-dir check, so the
    # pre-existing "no such account" message must be unchanged.
    _write_account(_store(home), "account-a")
    # Act
    result = switch_account("not-registered", home=home)
    # Assert
    assert "No account directory" in result["message"]


# ---------------------------------------------------------------------------
# save_account / delete_account — a store the caller cannot write
# ---------------------------------------------------------------------------


@_ROOT_SKIP
def test_save_account_refuses_with_a_message_naming_the_registry(
    home: Path, readonly_store: Path
) -> None:
    # Arrange — a store the process cannot write, standing in for the
    # read-only host registry bound into a container. Unguarded, this
    # surfaces as a bare errno naming a .tmp file.
    metadata = {"email_address": "x@example.test"}
    # Act
    save = lambda: save_account("new-account", metadata, home=home)
    # Assert
    with pytest.raises(SharedStoreWriteRefused, match=str(readonly_store)):
        save()


@_ROOT_SKIP
def test_save_account_refusal_points_at_the_host_as_the_single_writer(
    home: Path, readonly_store: Path
) -> None:
    # Arrange — the remedy must be in the message: the host is the
    # registry's single writer, and a container reaches it via the listen
    # bypass rather than by writing the mount.
    metadata = {"email_address": "x@example.test"}
    # Act
    save = lambda: save_account("new-account", metadata, home=home)
    # Assert
    with pytest.raises(SharedStoreWriteRefused, match="host_exec_local"):
        save()


def test_save_account_still_writes_a_normal_store(home: Path) -> None:
    # Arrange — the guard must cost the working path nothing.
    _store(home).mkdir(parents=True)
    # Act
    meta = save_account("new-account", {"email_address": "x@example.test"}, home=home)
    # Assert
    assert json.loads(meta.read_text(encoding="utf-8"))["name"] == "new-account"


@_ROOT_SKIP
def test_delete_account_refuses_to_report_a_removal_that_did_not_happen(
    home: Path, readonly_store_holding_account_a: Path
) -> None:
    # Arrange — the sharp edge: rmtree(ignore_errors=True) returned True
    # whether or not the directory went away.
    account = "account-a"
    # Act
    remove = lambda: delete_account(account, home=home)
    # Assert
    with pytest.raises(SharedStoreWriteRefused, match=account):
        remove()


@_ROOT_SKIP
def test_delete_account_leaves_the_account_in_place_when_it_cannot_remove_it(
    home: Path, readonly_store_holding_account_a: Path
) -> None:
    # Arrange — the refusal must describe reality: the account is still
    # registered afterwards.
    account_dir = readonly_store_holding_account_a / "account-a"
    # Act — the refusal itself is the test above's subject; this one asks
    # only whether reality matches what that refusal claims.
    with contextlib.suppress(SharedStoreWriteRefused):
        delete_account("account-a", home=home)
    # Assert
    assert account_dir.is_dir()


def test_delete_account_still_removes_from_a_normal_store(home: Path) -> None:
    # Arrange
    _write_account(_store(home), "account-a")
    # Act
    removed = delete_account("account-a", home=home)
    # Assert
    assert removed is True
