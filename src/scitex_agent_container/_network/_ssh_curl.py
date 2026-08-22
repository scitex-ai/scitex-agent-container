"""Generic ``ssh + remote curl`` POST helper (ADR-0015 Stage 2).

A single function that does what both the ``/v1/turn`` direct-ssh path
and the cross-host ``message:send`` forwarder need: open an ssh
connection (with ControlMaster reuse), invoke ``curl`` on the remote,
pipe a JSON body into curl's stdin, and surface the curl exit + stdout
+ stderr to the caller.

Why split this out of :mod:`peer`:

* Peer.py already hosts ``_post_turn_via_ssh`` for ``/v1/turn`` and
  was approaching the per-file LOC cap; keeping a focused sibling
  matches the ``_peer_resolve`` / ``_peer_timeout`` / ``_peer_dispatch``
  extraction pattern already in this package.
* The cross-host forwarder in ``_listen/_node_channel.py`` posts to
  ``/agents/<name>/message:send`` rather than ``/v1/turn``. Sharing the
  ssh wrapper instead of duplicating it keeps the ssh argv shape (and
  the ControlMaster options) byte-for-byte identical across both
  paths, so any future ssh hardening lands in one place.

Design notes
------------

* Body is bytes — callers serialize JSON themselves so this helper
  stays transport-agnostic (no implicit Content-Type assumption).
* Authorization is opt-in via ``bearer=``; when ``None`` no auth header
  is appended. Same shape as the existing ``/v1/turn`` curl invocation
  (which never carried a bearer).

SECURITY — why the bearer is not in the command
-----------------------------------------------
This helper used to splice the token into the remote command as
``-H 'Authorization: Bearer <value>'``. That string is an argument of
the LOCAL ``ssh`` process and, once dispatched, of the REMOTE shell and
``curl`` processes — and a process's argv is world-readable at
``/proc/<pid>/cmdline`` on Linux, unlike ``/proc/<pid>/environ`` which
the kernel restricts to the owning uid. So every cross-host
``message:send`` published the destination's ``peer-tokens/<host>.token``
to every local user on BOTH machines, for the life of the request. That
token is what makes the destination's ``NodeAuthMiddleware`` admit the
caller as administrative.

The sibling :mod:`.._hostsync._push_tokens_io` already refused to do
this: :func:`~.._hostsync._push_tokens_io.probe_peer_listen_auth` hands
its bearer to ``curl --config -`` on stdin for exactly this reason, and
:func:`~.._hostsync._push_tokens_io.write_peer_token` states the rule as
"the value rides stdin, never the argv, which is visible in the peer's
process table". This module now follows that precedent rather than
inventing a second shape.

The wrinkle the sibling does not have is that stdin is already spoken
for here: the BODY rides it (``-d @-``), and one pipe cannot carry both.
So the bearer path FRAMES the single stdin stream — first line the
token, remainder the body — and the remote snippet splits it with the
POSIX ``read`` builtin, which is specified not to consume past the
newline on a non-seekable input. The body is spooled to a ``0600``
``mktemp`` file (``umask 077``) and the token goes to ``curl --config -``
through a here-document, so the token reaches neither an argv nor a
named file, and the body keeps streaming without being encoded into
anything.

The no-bearer path is left BYTE-IDENTICAL: ``/v1/turn`` carries no
token, so it keeps the exact remote command (and ControlMaster reuse)
it has always had.
* The return value is ``(curl_exit_code, stdout_bytes, stderr_bytes)``;
  the caller decides how to interpret it (parse JSON / treat non-zero
  as 502 / inspect curl's `-w` output). This keeps the helper free of
  any HTTP-status assumption.
* ssh-level timeouts (TCP / sshd handshake) are layered on top of the
  per-call ``timeout_s``: the ``subprocess.run`` timeout is
  ``timeout_s + 15`` so a short remote-curl deadline still leaves room
  for ssh ControlMaster setup.
"""

from __future__ import annotations

import logging
import subprocess

__all__ = ["_post_via_ssh_curl"]

log = logging.getLogger(__name__)

#: Characters a bearer may not contain, and why. ``\n``/``\r`` would break
#: the stdin FRAMING (the token is the first line, the body is the rest), so
#: a token carrying one would spill into the request body. ``"`` and ``\``
#: are the two characters curl's config parser treats specially inside a
#: quoted value, so either would corrupt the header rather than authenticate.
#: Refusing loudly beats sending a silently mangled Authorization header.
_BEARER_FORBIDDEN = ('"', "\\", "\n", "\r")

def _remote_curl_with_bearer(*, port: int, path: str, timeout_s: float) -> str:
    """The remote snippet for an AUTHENTICATED post. Carries no token.

    Reads the framed stdin — ``<token>\\n<body>`` — with one ``read`` for
    the token, then spools the remainder to a ``0600`` temp file as the
    request body. ``curl --config -`` then takes the
    Authorization header from a here-document, so the value exists only in
    the remote shell's memory and on curl's stdin — never in any process's
    argv on either host.

    ``umask 077`` covers the ``mktemp`` body file; the exit status is
    curl's, preserved across the cleanup so the caller's ``rc`` semantics
    are unchanged. Quoting is double-quotes only, so the whole snippet
    survives being single-quoted by a dispatcher if one is ever added.
    """
    return (
        "umask 077\n"
        "sac_body=$(mktemp) || exit 1\n"
        'trap "rm -f $sac_body" EXIT HUP INT TERM\n'
        "IFS= read -r sac_bearer\n"
        'cat > "$sac_body"\n'
        f"curl -sS --max-time {int(timeout_s)} -X POST "
        '-H "Content-Type: application/json" '
        '--config - -d @"$sac_body" '
        f"http://127.0.0.1:{port}{path} <<SAC_CURL_CONFIG\n"
        'header = "Authorization: Bearer $sac_bearer"\n'
        "SAC_CURL_CONFIG\n"
    )


def _post_via_ssh_curl(
    *,
    host: str,
    port: int,
    path: str,
    body: bytes,
    bearer: str | None = None,
    timeout_s: float = 15.0,
) -> tuple[int, bytes, bytes]:
    """ssh into ``host`` and POST ``body`` to ``127.0.0.1:port{path}``.

    Returns ``(curl_exit_code, stdout, stderr)``. The exit code is the
    *ssh* process exit; ssh propagates curl's exit when the remote
    command runs, so a non-zero return generally means either an ssh
    transport failure (handshake / auth) or a curl failure on the
    remote (connect refused / DNS / TLS). Both surface as the same
    non-zero rc — the caller maps that uniformly to a 502.

    The body is piped through ssh stdin, so even multi-MB envelopes
    don't need to be encoded into the argv. The remote curl uses
    ``--max-time timeout_s`` so the per-call deadline is enforced *on
    the destination* in addition to the ssh-side ``subprocess.run``
    timeout (``timeout_s + 15``).

    ``bearer`` NEVER reaches an argv on either host. When set, stdin is
    framed as ``<token>\\n<body>`` and the remote snippet splits it (see
    the module docstring and :func:`_remote_curl_with_bearer`); a value
    containing a newline, ``"`` or ``\\`` is refused rather than sent
    mangled. When ``None`` the remote command and the stdin stream are
    both byte-identical to the pre-fix ``/v1/turn`` path.

    The argv shape (``-o BatchMode=yes -o ConnectTimeout=15`` plus
    :func:`scitex_agent_container._state.host_config.ssh_control_options`)
    matches the existing ``/v1/turn`` direct-ssh path verbatim so the
    same ControlMaster socket is reused by both transports.
    """
    if not host:
        raise ValueError("_post_via_ssh_curl: host must be non-empty")
    if not port or port <= 0:
        raise ValueError(f"_post_via_ssh_curl: port must be positive (got {port!r})")
    if not path or not path.startswith("/"):
        raise ValueError(f"_post_via_ssh_curl: path must start with '/' (got {path!r})")

    # Build the remote curl. Bearer is conditional so the ``/v1/turn``
    # path (no bearer) stays byte-identical to the pre-refactor argv.
    stdin_payload = body
    if bearer is None:
        remote_curl = (
            f"curl -sS --max-time {int(timeout_s)} "
            f"-X POST -H 'Content-Type: application/json' "
            f"-d @- "
            f"http://127.0.0.1:{port}{path}"
        )
    else:
        # The token must not reach any argv — see the module docstring.
        # It rides the FRONT of the stdin stream instead, so the remote
        # snippet below can read it without it ever being an argument.
        bad = [ch for ch in _BEARER_FORBIDDEN if ch in bearer]
        if bad:
            raise ValueError(
                "_post_via_ssh_curl: bearer must not contain "
                + ", ".join(repr(ch) for ch in bad)
                + " — the value is framed as the first line of ssh stdin and "
                "then read into a curl config header, and these characters "
                "would break the framing or the header parse. (The value "
                "itself is withheld from this message.)"
            )
        remote_curl = _remote_curl_with_bearer(
            port=port, path=path, timeout_s=timeout_s
        )
        stdin_payload = bearer.encode("utf-8") + b"\n" + body

    # Connection multiplexing — same options as the ``/v1/turn`` path.
    from .._state.host_config import ssh_control_options

    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        *ssh_control_options(),
        host,
        remote_curl,
    ]

    try:
        proc = subprocess.run(
            ssh_cmd,
            input=stdin_payload,
            capture_output=True,
            timeout=timeout_s + 15,
        )
    except subprocess.TimeoutExpired as exc:
        # Surface as a synthetic non-zero rc so callers have one branch
        # to handle. The stderr carries the diagnostic; rc=124 mirrors
        # GNU ``timeout``'s convention for "killed by timeout".
        stderr = (exc.stderr or b"") + (
            f"\nssh+curl timed out after {timeout_s:.0f}s".encode()
        )
        return (124, exc.stdout or b"", stderr)

    return (int(proc.returncode), proc.stdout or b"", proc.stderr or b"")
