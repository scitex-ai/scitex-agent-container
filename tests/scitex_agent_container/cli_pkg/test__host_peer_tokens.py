"""Peer credential changes need approval before the registry is inspected."""

import json
import stat
import sys
from contextlib import contextmanager

import pytest
from click.testing import CliRunner

from scitex_agent_container._listen import peer_tokens
from scitex_agent_container.cli_pkg._main import main

_SECRET = "owned-fixture-bearer"


@pytest.fixture
def peer_home(tmp_path, env_save_restore):
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    root = home / ".scitex/agent-container/peer-tokens"
    root.mkdir(parents=True)
    (root / "owned-peer.token").write_text(_SECRET)
    (root / "owned-peer.token").chmod(0o600)
    return root


def _snapshot(root):
    return {
        p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in root.iterdir()
    }


@contextmanager
def _no_registry_calls():
    """Observe real functions without replacing a provider or command symbol."""
    protected = {
        fn.__code__
        for fn in (
            peer_tokens.default_peer_tokens_dir,
            peer_tokens.peer_token_path,
            peer_tokens.write_peer_token,
            peer_tokens.read_peer_token,
            peer_tokens.list_peer_hosts,
        )
    }
    calls = []
    original = sys.getprofile()

    def observe(frame, event, arg):
        if event == "call" and frame.f_code in protected:
            calls.append(frame.f_code.co_name)
            raise RuntimeError("peer registry entered before approval")

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(original)


@pytest.mark.parametrize(
    "args",
    [
        ["add-peer", "new-peer", _SECRET],
        ["remove-peer", "owned-peer"],
    ],
)
@pytest.mark.parametrize(
    "ambient_approval", ["", "1"], ids=["no-ambient", "ambient-assume-yes"]
)
def test_unapproved_changes_refuse_before_registry_access(
    peer_home, args, env_save_restore, ambient_approval
):
    # Arrange
    before = _snapshot(peer_home)
    env_save_restore.set("SAC_ASSUME_YES", ambient_approval)
    # Act
    with _no_registry_calls() as calls:
        result = CliRunner().invoke(main, ["host", *args])
    # Assert
    assert (
        result.exit_code,
        calls,
        _snapshot(peer_home),
        _SECRET in result.output,
    ) == (
        2,
        [],
        before,
        False,
    )


@pytest.mark.parametrize(
    "args",
    [
        ["add-peer", "new-peer", _SECRET],
        ["remove-peer", "owned-peer"],
    ],
)
@pytest.mark.parametrize(
    "approval", [[], ["--yes"]], ids=["preview", "approved-preview"]
)
@pytest.mark.parametrize(
    "prefix,suffix",
    [([], ["--json"]), (["--json"], [])],
    ids=["local-json", "root-json"],
)
def test_argument_preview_hides_tokens_and_preserves_registry(
    peer_home, args, approval, prefix, suffix
):
    # Arrange
    before = _snapshot(peer_home)
    command = [*prefix, "host", *args, *suffix]
    # Act
    with _no_registry_calls() as calls:
        result = CliRunner().invoke(main, [*command, "--dry-run", *approval])
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        payload,
        calls,
        _snapshot(peer_home),
        _SECRET in result.output,
    ) == (
        0,
        {
            "dry_run": True,
            "action": args[0],
            "peer_host": args[1],
            "validated": False,
            "token_value_included": False,
        },
        [],
        before,
        False,
    )


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_approved_add_writes_private_token_without_printing_it(peer_home, approval):
    # Arrange
    destination = peer_home / "new-peer.token"
    # Act
    result = CliRunner().invoke(
        main, ["host", "add-peer", "new-peer", _SECRET, approval, "--json"]
    )
    payload = json.loads(result.stdout)
    # Assert
    assert (
        result.exit_code,
        destination.read_text(),
        stat.S_IMODE(destination.stat().st_mode),
        payload,
        _SECRET in result.output,
    ) == (
        0,
        _SECRET,
        0o600,
        {"action": "add-peer", "peer_host": "new-peer", "path": str(destination)},
        False,
    )


def test_approved_remove_is_idempotent_and_preserves_other_peers(peer_home):
    # Arrange
    keep = peer_home / "keep-peer.token"
    keep.write_text("other-owned-fixture")
    # Act
    first = CliRunner().invoke(
        main, ["host", "remove-peer", "owned-peer", "--yes", "--json"]
    )
    second = CliRunner().invoke(
        main, ["host", "remove-peer", "owned-peer", "-y", "--json"]
    )
    # Assert
    assert (
        first.exit_code,
        second.exit_code,
        (peer_home / "owned-peer.token").exists(),
        keep.read_text(),
        json.loads(first.stdout)["removed"],
        json.loads(second.stdout)["removed"],
    ) == (
        0,
        0,
        False,
        "other-owned-fixture",
        True,
        False,
    )


def test_remove_peer_refuses_traversal_without_removing_sibling_file(
    tmp_path, env_save_restore
):
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import host_group

    env_save_restore.set("HOME", str(tmp_path))
    root = tmp_path / ".scitex/agent-container/peer-tokens"
    root.mkdir(parents=True)
    outside = root.parent / "outside.token"
    outside.write_text("outside-fixture")
    # Act
    result = CliRunner().invoke(host_group, ["remove-peer", "../outside", "--yes"])
    content = outside.read_text() if outside.exists() else None
    # Assert
    assert (result.exit_code, content, "not a path" in result.output) == (
        2,
        "outside-fixture",
        True,
    )
