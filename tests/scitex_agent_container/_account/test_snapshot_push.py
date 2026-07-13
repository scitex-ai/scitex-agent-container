"""Tests for pushing a refreshed account snapshot to a peer.

No-mocks (PA-306 / STX-NM002). Every test drives the REAL production
transport: the real :class:`SshTransport` renders its argv through the
real ``build_ssh_argv``, the real ``subprocess.run`` resolves ``ssh``
through the real ``$PATH``. Only the NETWORK HOP is replaced — by a real
``ssh`` executable on ``$PATH`` that runs the post-``--`` remote command
LOCALLY, joined with spaces and re-parsed by a shell exactly as
OpenSSH + sshd do. That is the same honest-replacement technique as the
repo's existing ``ssh_http_shim`` helper.

Everything else is real: the remote ``mkdir`` / ``dd`` / ``chmod`` /
``stat`` / ``mv`` are the real coreutils, they operate on a real
directory tree under ``tmp_path``, the token bytes travel over a real
stdin pipe, and the 0600 assertion is made against a real file's real
mode as read back by the real ``stat``.

Peer resolution runs against a REAL ``config.yaml`` in ``tmp_path``,
pinned with ``$SCITEX_AGENT_CONTAINER_CONFIG`` — the same file
``sac host list`` reads.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import stat as stat_mod
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._account.snapshot_push import (
    FILE_MODE,
    STAGED_SUFFIX,
    SnapshotPushError,
    SshTransport,
    UnknownPeerError,
    parse_stat,
    push_snapshot,
    resolve_peer_transport,
    ssh_op_argv,
)

# A distinctive, greppable stand-in for token material. Every no-leak
# assertion in this module searches for these exact bytes.
_ACCESS = "ACCESS-TOKEN-MUST-NEVER-BE-PRINTED"
_REFRESH = "REFRESH-TOKEN-MUST-NEVER-BE-PRINTED"


# ---------------------------------------------------------------------------
# Real-binary shims (honest replacements — no mocks)
# ---------------------------------------------------------------------------

# Faithful to OpenSSH: everything after `--` is joined with spaces and fed
# to a shell on the far side. A future quoting bug therefore breaks this
# test exactly as it would break production.
_SSH_SHIM = r"""#!/bin/sh
printf '%s\0' "$@" | base64 | tr -d '\n' >> __LOG__
printf '\n' >> __LOG__
__seen=0
__cmd=
for __a in "$@"; do
  if [ "$__seen" = 1 ]; then
    __cmd="$__cmd $__a"
  elif [ "$__a" = "--" ]; then
    __seen=1
  fi
done
if [ "$__seen" != 1 ]; then
  echo "shim ssh: no -- separator in argv" >&2
  exit 2
fi
exec sh -c "$__cmd"
"""

# A `dd` whose stdin died mid-stream: it sees EOF early, writes a SHORT
# file and still exits 0. This is the real failure shape of a dropped ssh
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

# A `chmod` that reports success and changes nothing — the real behaviour
# of a CIFS / 9p / ACL-backed filesystem. The push must catch this by
# READING the mode back, not by trusting the chmod's exit code.
_LYING_CHMOD = r"""#!/bin/sh
exit 0
"""


class _ShimBin:
    """A real bin dir on ``$PATH`` holding real executables."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin = bin_dir
        self.ssh_log = bin_dir / "ssh.argv.log"

    def install(self, name: str, source: str) -> Path:
        script = self.bin / name
        script.write_text(source.replace("__LOG__", shlex.quote(str(self.ssh_log))))
        script.chmod(0o755)
        return script

    def ssh_invocations(self) -> list[list[str]]:
        if not self.ssh_log.exists():
            return []
        calls: list[list[str]] = []
        for line in self.ssh_log.read_text().splitlines():
            if not line:
                calls.append([])
                continue
            parts = base64.b64decode(line).split(b"\x00")
            if parts and parts[-1] == b"":
                parts = parts[:-1]
            calls.append([p.decode("utf-8", "surrogateescape") for p in parts])
        return calls


@pytest.fixture
def shim(tmp_path: Path) -> Iterator[_ShimBin]:
    """Prepend a real bin dir to ``$PATH``; install the executing ssh."""
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved}"
    controller = _ShimBin(bin_dir)
    controller.install("ssh", _SSH_SHIM)
    try:
        yield controller
    finally:
        os.environ["PATH"] = saved


# ---------------------------------------------------------------------------
# Real peer config (the same table `sac host list` reads)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Peer-config reuse (requirement: sac's EXISTING peer table, no new config)
# ---------------------------------------------------------------------------


def test_transport_resolves_peer_from_sac_config(peer_config) -> None:
    # Arrange / Act — the peer name is looked up in the real config.yaml.
    transport = resolve_peer_transport("spartan")
    # Assert
    assert transport.peers["spartan"].ssh == "ywatanabe@spartan-login1"


def test_unknown_peer_raises_naming_the_known_peers(peer_config) -> None:
    # Arrange / Act
    with pytest.raises(UnknownPeerError) as excinfo:
        resolve_peer_transport("not-a-peer")
    # Assert — the error lists the peers that ARE registered.
    assert "spartan" in str(excinfo.value)


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
    # Act — `via: [mba]` must become ssh's -J chain, resolved to mba's ssh.
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
# The happy path — real transfer, real mode, real verification
# ---------------------------------------------------------------------------


def test_push_lands_the_file_on_the_peer(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert — the bytes really arrived.
    assert remote.read_text() == local.read_text()


def test_push_lands_mode_0600(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert — the REAL mode of the REAL landed file.
    assert _mode_of(remote) == FILE_MODE


def test_push_reports_the_verified_mode_it_read_back(
    tmp_path, peer_config, shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    record = push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert — the mode in the record was READ OFF the peer, not assumed.
    assert record["mode"] == FILE_MODE


def test_push_hardens_the_account_directory(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert — 0700 dir closes the window before the file's own chmod lands.
    assert _mode_of(remote.parent) == "700"


def test_push_defaults_to_the_identical_absolute_path(
    tmp_path, peer_config, shim
) -> None:
    # Arrange — no remote_path override: the peer's path IS the local path.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    # Act
    record = push_snapshot(
        "work", local, transport=resolve_peer_transport("spartan")
    )
    # Assert
    assert record["remote_path"] == str(local)


def test_push_leaves_no_staging_file_behind(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert — the staged sibling was consumed by the atomic publish.
    assert not Path(str(remote) + STAGED_SUFFIX).exists()


def test_push_is_idempotent(tmp_path, peer_config, shim) -> None:
    # Arrange — the timer re-runs every 2h against an already-pushed peer.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    transport = resolve_peer_transport("spartan")
    push_snapshot("work", local, transport=transport, remote_path=str(remote))
    # Act — second run over the top of the first.
    record = push_snapshot(
        "work", local, transport=transport, remote_path=str(remote)
    )
    # Assert
    assert record["mode"] == FILE_MODE and remote.read_text() == local.read_text()


def test_push_reaches_the_peer_over_the_real_ssh_argv(
    tmp_path, peer_config, shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert — production really shelled out to ssh, at the configured peer.
    assert all(
        "ywatanabe@spartan-login1" in call for call in shim.ssh_invocations()
    )


# ---------------------------------------------------------------------------
# The 0600 guarantee is VERIFIED, not assumed
# ---------------------------------------------------------------------------


def test_lying_chmod_is_caught_and_fails_loud(tmp_path, peer_config, shim) -> None:
    # Arrange — a peer FS that accepts chmod and ignores it (CIFS / 9p / ACL).
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    staged = Path(str(remote) + STAGED_SUFFIX)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.touch()
    staged.chmod(0o644)  # dd truncates in place and keeps this mode
    shim.install("chmod", _LYING_CHMOD)
    # Act / Assert — reading the mode back is what catches it.
    with pytest.raises(SnapshotPushError, match="did not honour"):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(remote),
        )


def test_lying_chmod_never_publishes_the_token(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    staged = Path(str(remote) + STAGED_SUFFIX)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.touch()
    staged.chmod(0o644)
    shim.install("chmod", _LYING_CHMOD)
    # Act
    with pytest.raises(SnapshotPushError):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(remote),
        )
    # Assert — a token whose mode cannot be vouched for is never published.
    assert not remote.exists()


def test_lying_chmod_removes_the_staged_token(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    staged = Path(str(remote) + STAGED_SUFFIX)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.touch()
    staged.chmod(0o644)
    shim.install("chmod", _LYING_CHMOD)
    # Act
    with pytest.raises(SnapshotPushError):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(remote),
        )
    # Assert — no world-readable OAuth token is left lying on the peer.
    assert not staged.exists()


# ---------------------------------------------------------------------------
# Fail loud — truncated transfer, unreachable path
# ---------------------------------------------------------------------------


def test_truncated_transfer_fails_loud(tmp_path, peer_config, shim) -> None:
    # Arrange — an ssh stream that died mid-copy leaves dd exiting 0 short.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    shim.install("dd", _TRUNCATING_DD)
    # Act / Assert
    with pytest.raises(SnapshotPushError, match="truncated"):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(remote),
        )


def test_truncated_transfer_never_publishes(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    shim.install("dd", _TRUNCATING_DD)
    # Act
    with pytest.raises(SnapshotPushError):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(remote),
        )
    # Assert — a corrupt credential is never mv'd onto the live path.
    assert not remote.exists()


def test_unwritable_remote_dir_fails_loud_naming_the_peer(
    tmp_path, peer_config, shim
) -> None:
    # Arrange — a real ENOTDIR: the parent component is a regular file.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    blocker = tmp_path / "peer"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory")
    remote = blocker / "acct" / ".credentials.json"
    # Act / Assert — the message names the peer.
    with pytest.raises(SnapshotPushError, match="spartan"):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(remote),
        )


def test_missing_local_snapshot_fails_loud(tmp_path, peer_config, shim) -> None:
    # Arrange — nothing to push.
    local = tmp_path / "local" / "acct" / ".credentials.json"
    # Act / Assert
    with pytest.raises(SnapshotPushError, match="no snapshot"):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(tmp_path / "peer" / "acct" / ".credentials.json"),
        )


def test_unsafe_remote_path_is_refused_before_anything_is_sent(
    tmp_path, peer_config, shim
) -> None:
    # Arrange — a path the peer's login shell would re-split.
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    # Act / Assert
    with pytest.raises(SnapshotPushError, match="Nothing was sent"):
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path="/tmp/a b/.credentials.json",
        )


# ---------------------------------------------------------------------------
# Token values never leave this machine's memory in renderable form
# ---------------------------------------------------------------------------


def test_success_never_puts_a_token_on_a_command_line(
    tmp_path, peer_config, shim
) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert — the bytes rode stdin; no argv ever carried them.
    flat = " ".join(a for call in shim.ssh_invocations() for a in call)
    assert _ACCESS not in flat and _REFRESH not in flat


def test_success_record_carries_no_token(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    record = push_snapshot(
        "work",
        local,
        transport=resolve_peer_transport("spartan"),
        remote_path=str(remote),
    )
    # Assert
    rendered = json.dumps(record)
    assert _ACCESS not in rendered and _REFRESH not in rendered


def test_failure_message_carries_no_token(tmp_path, peer_config, shim) -> None:
    # Arrange
    local = _seed_snapshot(tmp_path / "local" / "acct" / ".credentials.json")
    blocker = tmp_path / "peer"
    blocker.write_text("not a directory")
    # Act
    with pytest.raises(SnapshotPushError) as excinfo:
        push_snapshot(
            "work",
            local,
            transport=resolve_peer_transport("spartan"),
            remote_path=str(blocker / "acct" / ".credentials.json"),
        )
    # Assert — errors name paths and accounts, never token material.
    assert _ACCESS not in str(excinfo.value) and _REFRESH not in str(excinfo.value)


# ---------------------------------------------------------------------------
# stat parsing (GNU + BSD)
# ---------------------------------------------------------------------------


def test_parse_stat_reads_the_gnu_form() -> None:
    # Arrange / Act / Assert — `stat -c %a:%s`
    assert parse_stat("600:1234\n") == ("600", 1234)


def test_parse_stat_normalises_a_leading_zero() -> None:
    # Arrange / Act / Assert — some coreutils print 0600 for `%Lp`.
    assert parse_stat("0600:12\n") == ("600", 12)


def test_parse_stat_rejects_unreadable_output() -> None:
    # Arrange / Act / Assert — an unparseable stat is a failure, not a guess.
    assert parse_stat("stat: cannot statx '/x'") is None


def test_transport_reports_a_missing_ssh_binary_as_a_failure(tmp_path) -> None:
    # Arrange — an SshTransport whose ssh cannot be executed at all.
    transport = SshTransport(
        peer="ghost",
        peers={
            "ghost": type(
                "P",
                (),
                {
                    "ssh": "nowhere",
                    "via": (),
                    "env_preamble": (),
                    "jump_chain": lambda self, peers: [],
                    "joined_preamble": lambda self: "",
                },
            )()
        },
    )
    # Act — no `ssh` shim installed and PATH is untouched, so this reaches the
    # real ssh, which cannot resolve host `nowhere` under BatchMode.
    result = transport.run(["true"])
    # Assert — a transport failure is a non-zero return, never a silent OK.
    assert result.returncode != 0
