"""Far-side operations for ``sac accounts keepalive`` (probe / backup / verify).

Split out of :mod:`.token_keepalive` (512-line cap) along a real seam: this
module owns everything that RUNS ON THE PEER, the other owns the local
guards and the orchestration order.

Every operation rides the SAME injectable
:class:`~.snapshot_push.PeerTransport` the snapshot push already uses, so
the peer's ``ssh:`` target, its ``via:`` ProxyJump chain and sac's
BatchMode / ControlMaster policy all come from the ONE peer table in
``~/.scitex/agent-container/config.yaml``. No second host config exists.

WHY A PROBE SCRIPT AND NOT A ONE-LINER
--------------------------------------
OpenSSH joins the post-host argv with spaces and the peer's login shell
re-parses the result, so a python ``-c`` one-liner would have to survive
two parses — and the original shell prototype paid for that with a
``curl -H "authorization: Bearer $T"``, which puts the ACCESS TOKEN in
the peer's process table for every user on that box to read.

So the probe is a FILE. It is streamed to the peer over the transport's
stdin (never an argv, never an environment variable), invoked with two
whitespace-free tokens (``state`` / ``verify`` plus a validated path),
reads the credential itself, and prints only an expiry in milliseconds,
opaque ``sha256:`` fingerprints, and an HTTP status code. A token value
never enters an argv, a log line, or this module's return values.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .snapshot_push import (
    DIR_MODE,
    FILE_MODE,
    PeerTransport,
    SnapshotPushError,
    _assert_safe_remote_path,
    _op,
)

#: Name of the probe, dropped beside the credential inside the 0700 account
#: directory and removed again by the caller's ``finally``. Fixed (not
#: random) so a crashed run leaves at most ONE stale file, which the next
#: run overwrites.
PROBE_NAME = ".sac-keepalive-probe.py"
#: The probe is executable-by-owner only; it is code, not data.
PROBE_MODE = "700"

#: The endpoint the far side must answer 200 on. Same one the shell
#: prototype curl'd — a cheap authenticated GET.
VERIFY_URL = "https://api.anthropic.com/v1/models"

# The probe. Pure stdlib (urllib, hashlib) so it needs nothing installed on
# the peer beyond the ``python3`` the prototype already required. It prints
# ONLY: an integer expiry, `sha256:<12hex>` fingerprints, `-`, `absent:1`,
# an HTTP status code, or `error:<ExceptionClass>`. Never token material.
_PROBE_SOURCE = """\
import hashlib
import json
import pathlib
import sys
import urllib.error
import urllib.request

VERIFY_URL = "__VERIFY_URL__"


def fp(value):
    if not value:
        return "-"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def main():
    mode = sys.argv[1]
    path = pathlib.Path(sys.argv[2])
    try:
        raw = json.loads(path.read_text())
    except Exception:
        print("absent:1")
        return 0
    oauth = raw.get("claudeAiOauth", raw)
    if not isinstance(oauth, dict):
        print("absent:1")
        return 0
    if mode == "state":
        try:
            expiry = int(oauth.get("expiresAt", 0))
        except (TypeError, ValueError):
            expiry = 0
        print("expiry:%d" % expiry)
        print("access_fp:%s" % fp(oauth.get("accessToken")))
        print("refresh_fp:%s" % fp(oauth.get("refreshToken")))
        return 0
    token = oauth.get("accessToken")
    if not token:
        print("notoken:1")
        return 0
    request = urllib.request.Request(
        VERIFY_URL,
        headers={
            "authorization": "Bearer " + token,
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print("http:%d" % response.status)
    except urllib.error.HTTPError as exc:
        print("http:%d" % exc.code)
    except Exception as exc:
        print("error:%s" % type(exc).__name__)
    return 0


sys.exit(main())
"""


def probe_source(verify_url: str | None = None) -> bytes:
    """The probe's bytes, with the verify endpoint substituted in.

    ``verify_url`` is a seam so a test can point the REAL probe at a REAL
    local HTTP server and exercise the real urllib call, rather than
    mocking the one step whose whole purpose is to make a real request.
    ``None`` resolves :data:`VERIFY_URL` AT CALL TIME — never as a default
    argument, which would bind the production endpoint at import and send
    a test's traffic to the live API.
    """
    url = verify_url if verify_url is not None else VERIFY_URL
    return _PROBE_SOURCE.replace("__VERIFY_URL__", url).encode("utf-8")


def probe_path(remote_path: str) -> str:
    """Where the probe lives on the peer: beside ``remote_path``."""
    return str(PurePosixPath(remote_path).parent / PROBE_NAME)


def ensure_remote_dir(transport: PeerTransport, remote_path: str) -> str:
    """``mkdir -p`` + ``chmod 700`` the peer's account directory.

    Hardened BEFORE anything is written into it, so neither the probe nor
    a staged credential can be world-readable for even the instant between
    creation under the remote umask and its own ``chmod``.
    """
    _assert_safe_remote_path(remote_path, transport.peer)
    remote_dir = str(PurePosixPath(remote_path).parent)
    _op(transport, ["mkdir", "-p", remote_dir], what="create", path=remote_dir)
    _op(transport, ["chmod", DIR_MODE, remote_dir], what="harden", path=remote_dir)
    return remote_dir


def install_probe(
    transport: PeerTransport, remote_path: str, *, verify_url: str | None = None
) -> str:
    """Stream the probe onto the peer (0700) and return its path."""
    path = probe_path(remote_path)
    _assert_safe_remote_path(path, transport.peer)
    _op(
        transport,
        ["dd", f"of={path}"],
        what="write",
        path=path,
        stdin=probe_source(verify_url),
    )
    _op(transport, ["chmod", PROBE_MODE, path], what="harden", path=path)
    return path


def remove_probe(transport: PeerTransport, remote_path: str) -> None:
    """Best-effort probe removal. Never raises — it runs in a ``finally``."""
    # stx-allow: fallback (reason: cleanup in a `finally`; a transport error
    # here must never replace the real diagnosis the caller is raising, and
    # a leftover 0700 probe carries no secret. The next run overwrites it.)
    try:
        transport.run(["rm", "-f", probe_path(remote_path)])
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass


def _run_probe(
    transport: PeerTransport, probe: str, mode: str, remote_path: str
) -> list[str]:
    """Run the probe in ``mode`` and return its non-blank output lines."""
    result = transport.run(["python3", probe, mode, remote_path])
    if result.returncode != 0:
        raise SnapshotPushError(
            f"cannot run the keepalive probe on peer '{transport.peer}': "
            f"`python3 {probe} {mode}` exited {result.returncode}. The peer "
            f"needs a python3 on PATH. Nothing was changed on {remote_path}."
        )
    return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]


def read_remote_state(
    transport: PeerTransport, probe: str, remote_path: str
) -> dict[str, object]:
    """Describe the credential currently living at ``remote_path``.

    Returns ``{"absent": bool, "expires_at_ms": int | None,
    "access_fp": str | None, "refresh_fp": str | None}`` — an expiry, two
    opaque fingerprints, nothing else. ``refresh_fp`` being non-``None`` is
    itself a finding: it means the peer is holding REFRESH material, i.e.
    it is a cloned session of the kind this command exists to stop.
    """
    lines = _run_probe(transport, probe, "state", remote_path)
    if not lines or lines[0] == "absent:1":
        return {
            "absent": True,
            "expires_at_ms": None,
            "access_fp": None,
            "refresh_fp": None,
        }
    fields: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep:
            fields[key] = value
    try:
        expiry = int(fields.get("expiry", "0"))
    except ValueError:
        expiry = 0

    def _fp(key: str) -> str | None:
        value = fields.get(key, "-")
        return None if value in ("", "-") else value

    return {
        "absent": False,
        "expires_at_ms": expiry or None,
        "access_fp": _fp("access_fp"),
        "refresh_fp": _fp("refresh_fp"),
    }


def verify_remote_token(transport: PeerTransport, probe: str, remote_path: str) -> int:
    """Return the HTTP status the PEER's own copy of the token receives.

    This is the load-bearing step: it proves the far side accepts what was
    just published BEFORE anything is restarted onto it. A prototype run
    that skipped it restarted a fleet onto an unverified token and wasted
    the round.

    Raises :class:`~.snapshot_push.SnapshotPushError` when the probe could
    not reach a status at all (DNS, TLS, proxy) — an unverifiable token is
    a failure, never an assumption.
    """
    lines = _run_probe(transport, probe, "verify", remote_path)
    head = lines[0] if lines else ""
    key, _sep, value = head.partition(":")
    if key == "http" and value.isdigit():
        return int(value)
    if key == "error":
        raise SnapshotPushError(
            f"peer '{transport.peer}' could not reach {VERIFY_URL} to verify "
            f"{remote_path} ({value}). NOT restarting anything."
        )
    raise SnapshotPushError(
        f"peer '{transport.peer}' returned no usable verification result for "
        f"{remote_path} (probe said {head!r}). NOT restarting anything."
    )


def backup_remote(
    transport: PeerTransport, remote_path: str, *, stamp: str
) -> str | None:
    """Copy the peer's CURRENT credential aside before it is replaced.

    Returns the backup path, or ``None`` when there was nothing to back up
    (first push to this peer). The copy is re-``chmod``-ed 0600 rather than
    trusted to ``cp -p``, for the same reason the push reads its mode back:
    a filesystem that ignores mode bits must not be left holding a
    world-readable OAuth token.
    """
    exists = transport.run(["test", "-f", remote_path])
    if exists.returncode != 0:
        return None
    backup = f"{remote_path}.bak-{stamp}"
    _assert_safe_remote_path(backup, transport.peer)
    _op(transport, ["cp", "-p", remote_path, backup], what="back up", path=backup)
    _op(transport, ["chmod", FILE_MODE, backup], what="harden", path=backup)
    return backup


__all__ = [
    "PROBE_MODE",
    "PROBE_NAME",
    "VERIFY_URL",
    "backup_remote",
    "ensure_remote_dir",
    "install_probe",
    "probe_path",
    "probe_source",
    "read_remote_state",
    "remove_probe",
    "verify_remote_token",
]
