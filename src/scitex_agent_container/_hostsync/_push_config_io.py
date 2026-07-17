"""Remote I/O for ``sac host push-config`` — read/write a peer's config.

The transport half of the push-config engine (:mod:`._push_config` holds
the verdicts and orchestration). Kept separate the same way
:mod:`._probe` is separate from :mod:`._sync`: the read path can be
audited as inert, and the single write snippet is small enough to read
whole.

Transport rules (both learned elsewhere in this package, kept here):

* The peer's config path is expanded REMOTELY (``$HOME`` inside the
  remote snippet). A locally-expanded ``~`` is the exact footgun this
  subsystem exists to kill — it yields the MASTER's home, not the
  peer's.
* File bytes come back base64-encoded between marker lines, the same
  protocol as :mod:`._probe`: a peer's motd/rc noise must never be
  mistaken for (or corrupt) config content, and truncated output is
  UNDETERMINED — never "absent", never "clean".
* Every snippet line is a complete command, because ssh joins the argv
  with spaces and a login shell may end up executing the lines directly
  (see :func:`.._state._host_ssh.build_ssh_argv` for the two parse
  modes a dispatched snippet must survive).
"""

from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass

from .._state.host_config import PeerSpec, build_ssh_argv

__all__ = [
    "MARKER",
    "REMOTE_CONFIG_DIR",
    "REMOTE_CONFIG_PATH",
    "RemoteConfigRead",
    "read_peer_config",
    "render_read_snippet",
    "render_write_snippet",
    "write_peer_config",
]

# Marker prefix for every parsed line, so peer motd/rc noise can never be
# mistaken for probe output (same discipline as ._probe.MARKER).
MARKER = "SAC_PUSHCFG"

# The peer-side location, expanded by the PEER's shell — never locally.
REMOTE_CONFIG_DIR = "$HOME/.scitex/agent-container"
REMOTE_CONFIG_PATH = f"{REMOTE_CONFIG_DIR}/config.yaml"


@dataclass(frozen=True)
class RemoteConfigRead:
    """One read of the peer's config file. ``ok=False`` == we do not know."""

    ok: bool
    absent: bool = False
    text: str = ""
    detail: str = ""


def render_read_snippet() -> str:
    """POSIX-sh read probe. Marker-framed; every line a complete command.

    ``__ABSENT__`` is POSITIVE evidence (the peer's own shell said the
    file is not there) — distinct from a transport failure, which never
    produces the ``end`` marker and therefore parses as UNDETERMINED.
    An existing-but-unreadable file gets its own marker rather than
    masquerading as either.
    """
    return (
        "\n"
        f"M={MARKER}\n"
        f'p="{REMOTE_CONFIG_PATH}"\n'
        'if [ ! -e "$p" ]; then echo "$M __ABSENT__"; '
        'elif [ -r "$p" ]; then '
        'echo "$M b64=$(base64 < "$p" | tr -d \'\\n\')"; '
        'else echo "$M unreadable"; fi\n'
        'echo "$M end"\n'
    )


def render_write_snippet(*, backup_stamp: str = "") -> str:
    """The remote write: content on stdin, tmp file, atomic ``mv``.

    ``backup_stamp`` non-empty = adopt mode: the existing file is copied
    to ``config.yaml.pre-adopt-<stamp>`` (peer-side) before the write.
    ``set -eu`` aborts the whole snippet before ``mv`` can land if any
    earlier step fails, and ``umask 077`` keeps the file private.
    """
    backup = ""
    if backup_stamp:
        backup = f'if [ -e "$p" ]; then cp -p "$p" "$p.pre-adopt-{backup_stamp}"; fi\n'
    return (
        "\nset -eu\n"
        "umask 077\n"
        f'd="{REMOTE_CONFIG_DIR}"\n'
        'p="$d/config.yaml"\n'
        'mkdir -p "$d"\n'
        f"{backup}"
        'cat > "$p.tmp"\n'
        'mv "$p.tmp" "$p"\n'
    )


def _parse_read(stdout: str, *, rc: int, stderr: str) -> RemoteConfigRead:
    """Parse marker lines into a :class:`RemoteConfigRead`.

    No ``end`` marker means the probe did not finish — UNDETERMINED,
    never "absent": rendering a dead transport as a missing file would
    make push mode CREATE a config over a peer we never actually saw.
    """
    absent = False
    unreadable = False
    b64: str | None = None
    saw_end = False
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith(MARKER + " "):
            continue
        body = line[len(MARKER) + 1 :]
        if body == "end":
            saw_end = True
        elif body == "__ABSENT__":
            absent = True
        elif body == "unreadable":
            unreadable = True
        elif body.startswith("b64="):
            b64 = body[len("b64=") :]
    if not saw_end:
        tail = (stderr or "").strip().splitlines()[-1:] or [""]
        return RemoteConfigRead(
            ok=False,
            detail=(
                "read probe returned no end marker — peer config state unknown "
                f"(ssh exit {rc}: {tail[0][:120]})"
            ),
        )
    if absent:
        return RemoteConfigRead(ok=True, absent=True)
    if unreadable:
        return RemoteConfigRead(
            ok=False,
            detail="config.yaml exists on the peer but is not readable",
        )
    if b64 is None:
        return RemoteConfigRead(
            ok=False, detail="read probe finished without a content marker"
        )
    try:
        text = base64.b64decode(b64, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        return RemoteConfigRead(
            ok=False,
            detail=f"could not decode the peer's config bytes ({exc})",
        )
    return RemoteConfigRead(ok=True, text=text)


def read_peer_config(
    peer: str,
    peers: dict[str, PeerSpec],
    *,
    timeout: int = 30,
    runner=subprocess.run,
) -> RemoteConfigRead:
    """Read the peer's config.yaml. Never raises, never writes.

    Rides :func:`build_ssh_argv` — sac's single remote-dispatch choke
    point — so the peer's ``via:`` ProxyJump chain and ``env_preamble``
    apply automatically.
    """
    try:
        argv = build_ssh_argv(
            peer,
            ["sh", "-c", render_read_snippet()],
            peers,
            extra_opts=["-o", f"ConnectTimeout={min(timeout, 15)}"],
        )
    except KeyError:
        return RemoteConfigRead(
            ok=False,
            detail=(
                f"peer '{peer}' is not defined in config.yaml — add it with:  "
                f"sac host add {peer} --ssh <user@host>"
            ),
        )
    try:
        proc = runner(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return RemoteConfigRead(ok=False, detail=f"ssh timed out after {timeout}s")
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: ssh spawn failure → UNKNOWN, never a false verdict)
        return RemoteConfigRead(
            ok=False, detail=f"ssh failed: {type(exc).__name__}: {exc}"
        )
    return _parse_read(proc.stdout or "", rc=proc.returncode, stderr=proc.stderr or "")


def write_peer_config(
    peer: str,
    peers: dict[str, PeerSpec],
    content: str,
    *,
    backup_stamp: str = "",
    timeout: int = 30,
    runner=subprocess.run,
) -> tuple[bool, str]:
    """Write ``content`` to the peer's config path. Returns ``(ok, detail)``.

    The content rides stdin (never the argv — an argv-embedded file body
    would be re-parsed by the remote shell), and the write lands via
    tmp-file + atomic ``mv`` so a dropped connection can never leave a
    half-written config.
    """
    try:
        argv = build_ssh_argv(
            peer,
            ["sh", "-c", render_write_snippet(backup_stamp=backup_stamp)],
            peers,
            extra_opts=["-o", f"ConnectTimeout={min(timeout, 15)}"],
        )
    except KeyError:
        return False, f"peer '{peer}' is not defined in config.yaml"
    try:
        proc = runner(
            argv,
            input=content,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"ssh timed out after {timeout}s"
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: ssh spawn failure → a LOUD failed push, never a claimed success)
        return False, f"ssh failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return False, f"remote write exited {proc.returncode}: {tail[0][:120]}"
    return True, ""
