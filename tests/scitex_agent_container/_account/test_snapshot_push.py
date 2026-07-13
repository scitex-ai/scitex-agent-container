"""Tests for pushing a refreshed account snapshot to a peer.

No-mocks (PA-306 / STX-NM002). Every test drives the REAL production
transport: the real :class:`SshTransport` renders its argv through the
real ``build_ssh_argv``, and the real ``subprocess.run`` resolves ``ssh``
through the real ``$PATH``. Only the NETWORK HOP is replaced — by the
``ssh_exec_shim`` helper, a real ``ssh`` executable on ``$PATH`` that runs
the post-``--`` remote command LOCALLY, joined with spaces and re-parsed by
a shell exactly as OpenSSH + sshd do. Same honest-replacement technique as
the repo's existing ``ssh_http_shim``.

Everything else is real: the remote ``mkdir`` / ``dd`` / ``chmod`` /
``stat`` / ``mv`` are the real coreutils, they operate on a real directory
tree under ``tmp_path``, the token bytes travel over a real stdin pipe, and
the 0600 assertion is made against a real file's real mode as read back by
the real ``stat``.

Peer resolution runs against a REAL ``config.yaml`` in ``tmp_path``, pinned
with ``$SCITEX_AGENT_CONTAINER_CONFIG`` — the same file ``sac host list``
reads.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
import stat as stat_mod
from pathlib import Path

import pytest

from scitex_agent_container._account.snapshot_push import (
    FILE_MODE,
    STAGED_SUFFIX,
    SnapshotPushError,
    UnknownPeerError,
    parse_stat,
    push_snapshot,
    resolve_peer_transport,
    ssh_op_argv,
)

# Distinctive, greppable stand-ins for token material. Every no-leak
# assertion below searches for these exact bytes.
_ACCESS = "ACCESS-TOKEN-MUST-NEVER-BE-PRINTED"
_REFRESH = "REFRESH-TOKEN-MUST-NEVER-BE-PRINTED"

# A `dd` whose stdin died mid-stream: it sees EOF early, writes a SHORT
# file and still exits 0. That is the real failure shape of a dropped ssh
# transfer, and the reason the push verifies SIZE as well as mode.
_TRUNCATING_DD = r"""#!/bin/sh
__of=
for __a in "$@"; do
  case "$__a" in
    of=*) __of=${__a#of=} ;;
  esac
done
head -c 5 > "$__of"
cat > /dev/null
exit 0
"""

# A `chmod` that reports success and changes nothing — the real behaviour of
# a CIFS / 9p / ACL-backed filesystem. The push must catch this by READING
# the mode back, not by trusting the chmod's exit code.
_LYING_CHMOD = """#!/bin/sh
exit 0
"""


@pytest.fixture
def peer_config(tmp_path: Path, env_save_restore) -> Path:
    """Write a real config.yaml and pin sac's peer lookup at it."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "peers:\n"
        "  mba:\n"
        "    ssh: ywatanabe@mba.local\n"
        "  spartan:\n"
        "    ssh: ywatanabe@spartan-login1\n"
        "    via: [mba]\n"
        "    env_preamble: |\n"
        "      module load Apptainer/1.3.3\n"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


def _seed_snapshot(path: Path) -> Path:
    """Write a realistic credentials snapshot carrying token material."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": _ACCESS,
                    "refreshToken": _REFRESH,
                    "expiresAt": 9_999_999_999_000,
                }
            }
        )
    )
    return path


def _mode_of(path: Path) -> str:
    return oct(stat_mod.S_IMODE(path.stat().st_mode))[2:]


def _push(local: Path, remote: Path | None = None):
    """Drive the REAL production transport at the configured peer."""
    return push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote) if remote is not None else None,
    )


def _push_error(local: Path, remote: Path | str) -> SnapshotPushError:
    """Run a push that MUST fail; return the raised error.

    Keeps the failure itself out of the test's assertion budget, so each
    fail-loud test can make exactly one assertion — on the message, or on
    the side effect (nothing published / nothing left behind).
    """
    try:
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(remote),
        )
    except SnapshotPushError as exc:
        return exc
    raise AssertionError(f"expected a SnapshotPushError pushing to {remote}")


def _resolve_error(peer: str) -> UnknownPeerError:
    """Resolve a peer that MUST be unknown; return the raised error."""
    try:
        resolve_peer_transport(peer)
    except UnknownPeerError as exc:
        return exc
    raise AssertionError(f"expected an UnknownPeerError for peer {peer!r}")


def _stage_a_0644_file(remote: Path) -> Path:
    """Pre-create the staged path 0644 — `dd` truncates and keeps the mode.

    Makes the lying-chmod tests independent of the ambient umask.
    """
    staged = Path(str(remote) + STAGED_SUFFIX)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.touch()
    staged.chmod(0o644)
    return staged


# ---------------------------------------------------------------------------
# Peer-config reuse (sac's EXISTING peer table — no second host config)
# ---------------------------------------------------------------------------


def test_transport_resolves_peer_from_sac_config(peer_config) -> None:
    # Arrange
    peer = "spartan"
    # Act — the name is looked up in the real config.yaml.
    transport = resolve_peer_transport(peer)
    # Assert
    assert transport.peers[peer].ssh == "ywatanabe@spartan-login1"


def test_unknown_peer_error_lists_the_registered_peers(peer_config) -> None:
    # Arrange
    peer = "not-a-peer"
    # Act
    error = _resolve_error(peer)
    # Assert — the error names the peers that ARE registered.
    assert "spartan" in str(error)


def test_ssh_argv_carries_the_peer_ssh_target(peer_config) -> None:
    # Arrange
    peers = resolve_peer_transport("spartan").peers
    # Act
    argv = ssh_op_argv("spartan", ["stat", "-c", "%a:%s", "/x"], peers)
    # Assert
    assert "ywatanabe@spartan-login1" in argv


def test_ssh_argv_carries_the_proxyjump_chain(peer_config) -> None:
    # Arrange
    peers = resolve_peer_transport("spartan").peers
    # Act — `via: [mba]` becomes ssh's -J chain, resolved to mba's ssh target.
    argv = ssh_op_argv("spartan", ["stat", "-c", "%a:%s", "/x"], peers)
    # Assert
    assert argv[argv.index("-J") + 1] == "ywatanabe@mba.local"


def test_ssh_argv_omits_the_lmod_env_preamble(peer_config) -> None:
    # Arrange
    peers = resolve_peer_transport("spartan").peers
    # Act — coreutils need no `module load`; wrapping them in one would make
    # a slow Lmod failure look like a push failure.
    argv = ssh_op_argv("spartan", ["stat", "-c", "%a:%s", "/x"], peers)
    # Assert
    assert not any("module load" in a for a in argv)


# ---------------------------------------------------------------------------
# Happy path — real transfer, real mode, real verification
# ---------------------------------------------------------------------------


def test_push_lands_the_file_on_the_peer(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _push(local, remote)
    # Assert — the bytes really arrived.
    assert remote.read_text() == local.read_text()


def test_push_lands_mode_0600(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _push(local, remote)
    # Assert — the REAL mode of the REAL landed file.
    assert _mode_of(remote) == FILE_MODE


def test_push_reports_the_verified_mode_it_read_back(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    record = _push(local, remote)
    # Assert — the mode in the record was READ OFF the peer, not assumed.
    assert record["mode"] == FILE_MODE


def test_push_hardens_the_account_directory(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _push(local, remote)
    # Assert — an 0700 dir closes the window before the file's own chmod lands.
    assert _mode_of(remote.parent) == "700"


def test_push_defaults_to_the_identical_absolute_path(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange — no remote_path override: the peer's path IS the local path.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    # Act
    record = _push(local)
    # Assert
    assert record["remote_path"] == str(local)


def test_push_leaves_no_staging_file_behind(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _push(local, remote)
    # Assert — the staged sibling was consumed by the atomic publish.
    assert not Path(str(remote) + STAGED_SUFFIX).exists()


def test_push_is_idempotent(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange — the refresher re-runs on a cadence, over an already-pushed peer.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    _push(local, remote)
    # Act — second run straight over the first.
    record = _push(local, remote)
    # Assert
    assert record["mode"] == FILE_MODE and remote.read_text() == local.read_text()


def test_push_reaches_the_peer_over_the_real_ssh_argv(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _push(local, remote)
    # Assert — production really shelled out to ssh, at the configured peer.
    assert all(
        "ywatanabe@spartan-login1" in call for call in ssh_exec_shim.invocations()
    )


# ---------------------------------------------------------------------------
# The 0600 guarantee is VERIFIED, not assumed
# ---------------------------------------------------------------------------


def test_lying_chmod_is_caught_and_fails_loud(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange — a peer FS that accepts chmod and ignores it (CIFS / 9p / ACL).
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    _stage_a_0644_file(remote)
    ssh_exec_shim.install_binary("chmod", _LYING_CHMOD)
    # Act
    error = _push_error(local, remote)
    # Assert — reading the mode back is what catches it.
    assert "did not honour" in str(error)


def test_lying_chmod_never_publishes_the_token(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    _stage_a_0644_file(remote)
    ssh_exec_shim.install_binary("chmod", _LYING_CHMOD)
    # Act
    _push_error(local, remote)
    # Assert — a token whose mode cannot be vouched for is never published.
    assert not remote.exists()


def test_lying_chmod_removes_the_staged_token(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    staged = _stage_a_0644_file(remote)
    ssh_exec_shim.install_binary("chmod", _LYING_CHMOD)
    # Act
    _push_error(local, remote)
    # Assert — no world-readable OAuth token is left lying on the peer.
    assert not staged.exists()


# ---------------------------------------------------------------------------
# Fail loud — truncated transfer, unusable remote path, missing snapshot
# ---------------------------------------------------------------------------


def test_truncated_transfer_fails_loud(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange — an ssh stream that died mid-copy leaves dd exiting 0, short.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    ssh_exec_shim.install_binary("dd", _TRUNCATING_DD)
    # Act
    error = _push_error(local, remote)
    # Assert
    assert "truncated" in str(error)


def test_truncated_transfer_never_publishes(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    ssh_exec_shim.install_binary("dd", _TRUNCATING_DD)
    # Act
    _push_error(local, remote)
    # Assert — a corrupt credential is never mv'd onto the live path.
    assert not remote.exists()


def test_unusable_remote_dir_fails_loud_naming_the_peer(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange — a real ENOTDIR: the parent component is a regular file.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    blocker = tmp_path / "peer"
    blocker.write_text("not a directory")
    # Act
    error = _push_error(local, blocker / "acct" / ".credentials.json")
    # Assert
    assert "spartan" in str(error)


def test_unusable_remote_dir_fails_loud_naming_the_path(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    blocker = tmp_path / "peer"
    blocker.write_text("not a directory")
    # Act
    error = _push_error(local, blocker / "acct" / ".credentials.json")
    # Assert
    assert str(blocker / "acct") in str(error)


def test_missing_local_snapshot_fails_loud(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange — nothing to push.
    local = tmp_path / "local" / "acct" / ".credentials.json"
    # Act
    error = _push_error(local, tmp_path / "peer" / "acct" / ".credentials.json")
    # Assert
    assert "no snapshot" in str(error)


def test_unsafe_remote_path_is_refused_before_anything_is_sent(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange — a path the peer's login shell would re-split.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    # Act
    error = _push_error(local, "/tmp/a b/.credentials.json")
    # Assert
    assert "Nothing was sent" in str(error)


# ---------------------------------------------------------------------------
# Token values are never rendered
# ---------------------------------------------------------------------------


def test_success_never_puts_a_token_on_a_command_line(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _push(local, remote)
    # Assert — the bytes rode stdin; no argv ever carried them.
    flat = " ".join(a for call in ssh_exec_shim.invocations() for a in call)
    assert _ACCESS not in flat and _REFRESH not in flat


def test_success_record_carries_no_token(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    record = _push(local, remote)
    # Assert
    rendered = json.dumps(record)
    assert _ACCESS not in rendered and _REFRESH not in rendered


def test_failure_message_carries_no_token(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    blocker = tmp_path / "peer"
    blocker.write_text("not a directory")
    # Act
    error = _push_error(local, blocker / "acct" / ".credentials.json")
    # Assert — errors name paths and accounts, never token material.
    assert _ACCESS not in str(error) and _REFRESH not in str(error)


# ---------------------------------------------------------------------------
# stat parsing (GNU + BSD)
# ---------------------------------------------------------------------------


def test_parse_stat_reads_the_gnu_form() -> None:
    # Arrange — what `stat -c %a:%s` prints.
    line = "600:1234\n"
    # Act
    parsed = parse_stat(line)
    # Assert
    assert parsed == ("600", 1234)


def test_parse_stat_normalises_a_leading_zero() -> None:
    # Arrange — some coreutils print 0600 for `%Lp`.
    line = "0600:12\n"
    # Act
    parsed = parse_stat(line)
    # Assert
    assert parsed == ("600", 12)


def test_parse_stat_rejects_unreadable_output() -> None:
    # Arrange — an unparseable stat must be a failure, never a guess.
    line = "stat: cannot statx '/x'"
    # Act
    parsed = parse_stat(line)
    # Assert
    assert parsed is None
