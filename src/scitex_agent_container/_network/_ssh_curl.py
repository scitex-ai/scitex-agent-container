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

    The body is piped through ssh stdin into curl's ``-d @-``, so even
    multi-MB envelopes don't need to be encoded into the argv. The
    remote curl uses ``--max-time timeout_s`` so the per-call deadline
    is enforced *on the destination* in addition to the ssh-side
    ``subprocess.run`` timeout (``timeout_s + 15``).

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
    auth_part = ""
    if bearer is not None:
        # Bearer values are operator-minted secrets — pass through a
        # single-quoted shell literal. We refuse any value carrying a
        # single quote because that would let the value break the
        # quoting; loud failure beats silent argv injection.
        if "'" in bearer:
            raise ValueError(
                "_post_via_ssh_curl: bearer must not contain single quotes"
            )
        auth_part = f"-H 'Authorization: Bearer {bearer}' "

    remote_curl = (
        f"curl -sS --max-time {int(timeout_s)} "
        f"-X POST -H 'Content-Type: application/json' "
        f"{auth_part}-d @- "
        f"http://127.0.0.1:{port}{path}"
    )

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
            input=body,
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
