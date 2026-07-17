"""Push a freshly-refreshed account snapshot to a PEER's identical path.

WHY — the gap this closes
-------------------------
Under the master-host single-refresher model (see
:mod:`scitex_agent_container.runtimes._apptainer_creds`) the host-side
``sac.accounts-refresh`` timer is the SOLE refresher: agents bind the
per-account snapshot ``:ro`` and never rotate the single-use OAuth
refresh_token themselves. One refresh serves every agent pinned to that
account, because the token is fungible across them.

That model holds only for agents on the SAME machine as the timer — they
share the snapshot's filesystem. An agent on a REMOTE peer does not.
Spartan having a ``/home/ywatanabe`` too is a coincidence of layout, not
a shared mount: a sac agent there binds *Spartan's own* copy of
``~/.scitex/agent-container/accounts/<acct>/.credentials.json``, which
NOTHING refreshes. It silently 401s within one access-token lifetime
(~8h). This module is the missing leg — after the timer rotates a
snapshot, copy it to the peer at the identical absolute path.

SECURITY CONTRACT
-----------------
* The remote file MUST land mode 0600, and the mode is READ BACK off the
  peer and asserted (:data:`FILE_MODE`). A flag is never trusted: a
  filesystem that silently ignores ``chmod`` (CIFS / 9p / some ACL
  mounts) must not be left holding a world-readable OAuth token on a
  shared HPC filesystem.
* Nothing is PUBLISHED before it is VERIFIED. The bytes land on a staged
  sibling path inside an 0700 directory; only a staged file whose mode
  AND size both check out is atomically ``mv``-ed onto the live path. A
  failed check removes the staged file and leaves the peer's previous
  snapshot untouched.
* Token VALUES never appear in an argv, an environment variable, stdout,
  stderr, an exception, or a log line. The bytes travel exactly once —
  over the transport's stdin, into the remote ``dd``. Only account names
  and paths are ever rendered. Captured stderr comes from ssh/coreutils,
  which diagnose in terms of paths, never file content.

FAIL LOUD
---------
Every failure raises :class:`SnapshotPushError` naming the peer and the
path. There is no silent-success path: a silent failure would recreate
exactly the invisible-staleness bug this module exists to kill.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .._state.host_config import PeerSpec

# The one mode an OAuth token snapshot may hold on a shared filesystem.
FILE_MODE = "600"
# The account directory is hardened FIRST, so that even the window between
# ``dd`` creating the staged file (under the remote umask) and its own
# ``chmod`` cannot expose the token to another user.
DIR_MODE = "700"
# Staged sibling of the live path — same directory, therefore the same
# filesystem, therefore ``mv`` is an atomic rename (no torn read by an
# agent that happens to open the snapshot mid-push). Fixed (not random)
# so a crashed run leaves at most ONE stale staging file, which the next
# run overwrites: idempotent, no clutter.
STAGED_SUFFIX = ".sac-push-tmp"

_DEFAULT_TIMEOUT_S = 120.0
_STDERR_TAIL = 400

# OpenSSH joins the post-host argv with spaces and the remote LOGIN SHELL
# re-parses the result, so a remote path carrying whitespace or a shell
# metacharacter would be re-split there. sac's own account paths never
# contain one; anything else is REFUSED rather than quoted by guesswork.
_SAFE_REMOTE_PATH = re.compile(r"\A[A-Za-z0-9._@%+:/-]+\Z")


class SnapshotPushError(RuntimeError):
    """A push could not be completed AND verified.

    Carries the peer and the path. NEVER carries token material.
    """


class UnknownPeerError(SnapshotPushError):
    """``--push-to <peer>`` named a peer sac's peer config does not know."""


@dataclasses.dataclass(frozen=True)
class RunResult:
    """Outcome of ONE remote operation. ``stdout``/``stderr`` are decoded."""

    returncode: int
    stdout: str
    stderr: str


class PeerTransport(Protocol):
    """The injectable transfer seam.

    One method: run a metachar-free argv on the peer, optionally feeding it
    bytes on stdin. :class:`SshTransport` is the real (default)
    implementation; every operation this module performs — ``mkdir``,
    ``chmod``, ``dd``, ``stat``, ``mv``, ``rm`` — goes through it, so a
    caller can substitute the transport without any of the push logic,
    the 0600 verification or the fail-loud contract changing shape.
    """

    peer: str

    def run(
        self, command: Sequence[str], *, stdin: bytes | None = None
    ) -> RunResult: ...


def _peers_with_lean_preamble(
    peer: str, peers: Mapping[str, "PeerSpec"]
) -> dict[str, "PeerSpec"]:
    """Return ``peers`` with ``peer``'s ``env_preamble`` stripped.

    The preamble exists to put ``apptainer`` on $PATH behind Lmod (Spartan)
    and is right for a dispatched ``sac`` invocation. It is WRONG here: it
    would wrap every ``mkdir`` / ``stat`` in a multi-second ``module load``
    chain whose failure — joined by ``&&`` — would then be misreported as a
    push failure. The operations in this module are coreutils, present on
    the default PATH of any peer.

    The concrete (glob-resolved) peer is re-inserted under its own name so
    a pattern peer such as ``spartan-*`` keeps working, and the map type is
    preserved so ``via:`` chain lookups keep their glob semantics.
    """
    from .._state.host_config import PeersMap

    spec = peers[peer]  # KeyError => the caller failed to validate the peer
    lean = PeersMap(peers)
    lean[peer] = dataclasses.replace(spec, env_preamble=())
    return lean


def ssh_op_argv(
    peer: str, command: Sequence[str], peers: Mapping[str, "PeerSpec"]
) -> list[str]:
    """Render the ssh argv for ONE remote operation.

    Delegates wholly to :func:`.._state.host_config.build_ssh_argv` — the
    same renderer ``sac host exec`` and cross-host agent dispatch use — so
    the peer's ``ssh:`` target, its ``via:`` ProxyJump chain, sac's
    BatchMode / accept-new-TOFU policy and its ControlMaster multiplexing
    all come from the ONE existing peer table
    (``~/.scitex/agent-container/config.yaml``). No second host config is
    invented.
    """
    from .._state.host_config import build_ssh_argv

    return build_ssh_argv(
        peer, list(command), _peers_with_lean_preamble(peer, peers)
    )


@dataclasses.dataclass(frozen=True)
class SshTransport:
    """The real transport: run one argv on ``peer`` over ssh.

    Bytes destined for the peer ride on stdin (never on the command line
    and never through the environment), so a token value cannot leak into
    a process table, a shell history or a journal line.
    """

    peer: str
    peers: Mapping[str, "PeerSpec"]
    timeout_s: float = _DEFAULT_TIMEOUT_S

    def run(
        self, command: Sequence[str], *, stdin: bytes | None = None
    ) -> RunResult:
        argv = ssh_op_argv(self.peer, command, self.peers)
        try:
            proc = subprocess.run(
                argv,
                input=stdin if stdin is not None else b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=124,
                stdout="",
                stderr=(
                    f"ssh to peer '{self.peer}' timed out after "
                    f"{self.timeout_s:g}s running `{command[0]}`"
                ),
            )
        except OSError as exc:
            return RunResult(returncode=127, stdout="", stderr=f"{exc}")
        return RunResult(
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", "replace"),
            stderr=proc.stderr.decode("utf-8", "replace"),
        )


def resolve_peer_transport(peer: str) -> SshTransport:
    """Build the production transport for ``peer`` from sac's peer config.

    Reads the SAME table behind ``sac host list`` /
    ``~/.scitex/agent-container/config.yaml`` (:func:`.._state.host_config.load`).
    An unregistered peer raises :class:`UnknownPeerError` listing the peers
    that ARE registered — and the caller is expected to do this BEFORE any
    refresh runs, so a typo'd peer name never costs a single-use OAuth
    refresh_token rotation.
    """
    from .._state.host_config import load as load_host_config

    cfg = load_host_config()
    if peer not in cfg.peers:
        known = ", ".join(sorted(cfg.peers)) or "(none registered)"
        raise UnknownPeerError(
            f"unknown peer '{peer}': not found under peers: in "
            f"{cfg.source_path}. Registered peers: {known}. "
            f"Add one with `sac host add {peer} --ssh <user@host>`."
        )
    return SshTransport(peer=peer, peers=cfg.peers)


def _tail(stderr: str) -> str:
    """Render a transport's stderr for an error message (paths only)."""
    text = (stderr or "").strip()
    if not text:
        return ""
    if len(text) > _STDERR_TAIL:
        text = text[:_STDERR_TAIL] + "…"
    return f": {text}"


def _assert_safe_remote_path(path: str, peer: str) -> None:
    """Refuse a remote path the remote shell would re-split. Fail loud."""
    if not _SAFE_REMOTE_PATH.match(path):
        raise SnapshotPushError(
            f"refusing to push to peer '{peer}': remote path {path!r} carries "
            "whitespace or a shell metacharacter, which the peer's login "
            "shell would re-split. Nothing was sent."
        )


def _op(
    transport: PeerTransport,
    command: Sequence[str],
    *,
    what: str,
    path: str,
    stdin: bytes | None = None,
) -> RunResult:
    """Run one remote operation; a non-zero exit is a loud failure."""
    result = transport.run(command, stdin=stdin)
    if result.returncode != 0:
        raise SnapshotPushError(
            f"failed to {what} {path} on peer '{transport.peer}' "
            f"(`{command[0]}` exited {result.returncode})"
            f"{_tail(result.stderr)}"
        )
    return result


def parse_stat(stdout: str) -> tuple[str, int] | None:
    """Parse a ``<mode>:<size>`` stat line into ``("600", 1234)``.

    ``None`` when the output is missing or unparseable — the caller then
    fails loud rather than assuming a mode it could not read.

    The mode is normalised by stripping leading zeroes so the GNU (``600``)
    and BSD (``0600`` on some coreutils) spellings compare equal.
    """
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    mode_s, sep, size_s = lines[0].partition(":")
    if not sep:
        return None
    mode_s = mode_s.strip().lstrip("0") or "0"
    size_s = size_s.strip()
    if not mode_s.isdigit() or not size_s.isdigit():
        return None
    return mode_s, int(size_s)


def stat_remote(transport: PeerTransport, path: str) -> tuple[str, int]:
    """Return ``(mode_octal, size_bytes)`` READ BACK off the peer.

    ``stat -c %a:%s`` is the GNU form (any Linux peer, Spartan included);
    ``stat -f %Lp:%z`` is the BSD form (a macOS peer such as ``mba``). The
    ``:``-joined format keeps the whole thing ONE whitespace-free token:
    OpenSSH joins the post-host argv with spaces and the remote shell
    re-parses it, so a format string containing a space would be re-split
    and ``%s`` taken for a second FILE operand.

    Raises :class:`SnapshotPushError` when the mode cannot be READ — an
    unverifiable mode is a failure, never an assumption.
    """
    last: RunResult | None = None
    for flag, fmt in (("-c", "%a:%s"), ("-f", "%Lp:%z")):
        last = transport.run(["stat", flag, fmt, path])
        if last.returncode == 0:
            parsed = parse_stat(last.stdout)
            if parsed is not None:
                return parsed
    raise SnapshotPushError(
        f"cannot verify the mode of {path} on peer '{transport.peer}': "
        f"`stat` did not return a readable <mode>:<size>"
        f"{_tail(last.stderr if last else '')}"
    )


def _discard(transport: PeerTransport, path: str) -> None:
    """Best-effort removal of a file whose mode we could not vouch for.

    Never raises — it runs on a failure path that is already going to
    raise, and a failed cleanup must not mask the original diagnosis.
    """
    # stx-allow: fallback (reason: cleanup on an already-failing path; a
    # transport error here must not replace the real error the caller is
    # about to raise. The failure to clean up is reported by that error's
    # own message pointing at the path.)
    try:
        transport.run(["rm", "-f", path])
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass


def push_snapshot(
    account: str,
    local_path: Path | str,
    *,
    transport: PeerTransport,
    remote_path: str | None = None,
) -> dict[str, Any]:
    """Copy one refreshed snapshot to the peer, at the IDENTICAL abs path.

    Steps, in the order that makes each failure safe:

    1. ``mkdir -p`` + ``chmod 700`` the remote account directory, so the
       staged file cannot be world-readable even for the instant between
       ``dd`` creating it under the remote umask and its own ``chmod``.
    2. Stream the bytes into a STAGED sibling (never onto the live path)
       and ``chmod 600`` it.
    3. VERIFY the staged file by reading its mode AND size back off the
       peer. Size matters as much as mode: a dropped ssh stream leaves the
       remote ``dd`` exiting 0 on a TRUNCATED file, which would publish a
       corrupt credential. Any mismatch removes the staged file and raises
       — the peer keeps its previous snapshot, untouched.
    4. Publish with an atomic same-directory ``mv -f``.
    5. Prove the LIVE path is what was verified. ``mv`` preserves mode and
       contents, so a mismatch here means the peer's filesystem mutated the
       file on rename — hostile; the file is removed rather than left as a
       token whose mode cannot be vouched for. A ``stat`` that cannot RUN
       at all (transport failure) still fails the run loud but does NOT
       delete: that same inode was already proven 0600 and complete before
       the rename, so deleting would strip the peer of its credentials for
       no security gain.

    Args:
        account: account name, for the returned record + error messages.
        local_path: the freshly-rotated snapshot on THIS host. Must be an
            absolute path to an existing file.
        transport: the transfer seam. Defaults are supplied by the caller
            (:func:`resolve_peer_transport` builds the real ssh one).
        remote_path: override the destination. Defaults to the IDENTICAL
            absolute path — a peer whose layout matches (the fleet's
            ``/home/ywatanabe`` convention) needs no mapping.

    Returns:
        ``{"account", "peer", "local_path", "remote_path", "mode", "bytes"}``
        — paths, an account name and a verified mode. Never a token.

    Raises:
        SnapshotPushError: on ANY failure, naming the peer and the path.
    """
    local = Path(local_path)
    if not local.is_absolute():
        raise SnapshotPushError(
            f"refusing to push account '{account}' to peer "
            f"'{transport.peer}': local snapshot path {local!s} is not "
            "absolute, so the peer's identical path cannot be derived."
        )
    if not local.is_file():
        raise SnapshotPushError(
            f"refusing to push account '{account}' to peer "
            f"'{transport.peer}': no snapshot at {local!s}."
        )

    remote = remote_path if remote_path is not None else str(local)
    _assert_safe_remote_path(remote, transport.peer)
    staged = remote + STAGED_SUFFIX
    remote_dir = str(PurePosixPath(remote).parent)

    payload = local.read_bytes()
    size = len(payload)

    # 1. An 0700 directory FIRST.
    _op(transport, ["mkdir", "-p", remote_dir], what="create", path=remote_dir)
    _op(transport, ["chmod", DIR_MODE, remote_dir], what="harden", path=remote_dir)

    # 2-3. Stage, harden, verify — nothing is published unverified.
    try:
        _op(
            transport,
            ["dd", f"of={staged}"],
            what="write",
            path=staged,
            stdin=payload,
        )
        _op(transport, ["chmod", FILE_MODE, staged], what="harden", path=staged)
        mode, remote_size = stat_remote(transport, staged)
        if mode != FILE_MODE:
            raise SnapshotPushError(
                f"refusing to publish account '{account}' on peer "
                f"'{transport.peer}': {staged} landed mode 0{mode}, not "
                f"0{FILE_MODE}. The peer's filesystem did not honour "
                "`chmod`; an OAuth token must never be readable by another "
                "user there. The staged file is being removed and the "
                "peer's previous snapshot is untouched."
            )
        if remote_size != size:
            raise SnapshotPushError(
                f"refusing to publish account '{account}' on peer "
                f"'{transport.peer}': {staged} is {remote_size} bytes, "
                f"expected {size} — the transfer was truncated. The staged "
                "file is being removed and the peer's previous snapshot is "
                "untouched."
            )
    except SnapshotPushError:
        _discard(transport, staged)
        raise

    # 4. Publish atomically.
    _op(transport, ["mv", "-f", staged, remote], what="publish", path=remote)

    # 5. Prove the LIVE path. (See the docstring for why a mode/size
    # mismatch deletes but an unreachable `stat` does not.)
    mode, remote_size = stat_remote(transport, remote)
    if mode != FILE_MODE or remote_size != size:
        _discard(transport, remote)
        raise SnapshotPushError(
            f"account '{account}' published to peer '{transport.peer}' at "
            f"{remote} did NOT verify (mode 0{mode}, {remote_size} bytes; "
            f"expected 0{FILE_MODE}, {size} bytes). The file has been "
            "removed from the peer rather than left unverified."
        )

    return {
        "account": account,
        "peer": transport.peer,
        "local_path": str(local),
        "remote_path": remote,
        "mode": mode,
        "bytes": size,
    }


__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "STAGED_SUFFIX",
    "PeerTransport",
    "RunResult",
    "SnapshotPushError",
    "SshTransport",
    "UnknownPeerError",
    "parse_stat",
    "push_snapshot",
    "resolve_peer_transport",
    "ssh_op_argv",
    "stat_remote",
]
