"""Keep a peer's ACCESS-ONLY credential alive — the engine behind ``keepalive``.

WHY THIS EXISTS (measured, 2026-08-10)
--------------------------------------
The fleet died three times in one day on ``401 OAuth access token has been
revoked``. Fingerprinting the credential material across hosts — values
never read — showed why::

    host              refreshToken fp   accessToken fp
    laptop            7fcccfdcdf9f      571181f2ae95
    scitex-compute-04 7fcccfdcdf9f      571181f2ae95   <- IDENTICAL
    scitex-nas-03     0ca3dd5e0632      705b3573d08b   <- separate session

The laptop and compute-04 were the SAME OAuth session cloned onto two
machines, REFRESH TOKEN INCLUDED. An OAuth refresh rotates the refresh
token and invalidates the previous access token, so whichever host
refreshed first silently revoked the other. There is no error on the
losing side; it simply starts 401ing.

The invariant is now REAL, not aspirational: ``scitex-nas-03`` holds the
fleet's ONLY refresh token, and the laptop and compute-04 have been
stripped to access-only and verified by fingerprint. That state is not
self-sustaining — an access-only host cannot renew what it holds, so
without this module those hosts simply expire.

ORDER IS THE WHOLE VALUE
------------------------
Each step exists because skipping it cost a fleet outage:

1. **COPY, never mint anew.** Minting ROTATES, and rotation revokes the
   token every running agent is currently holding — that is what killed
   ten agents on the morning of 2026-08-10. The master's CURRENT stored
   token is read and copied; nothing is refreshed here, and no attempt is
   ever made to PROVOKE a refresh.
2. **Refuse below ~300s of validity.** A token that expires in flight is
   worse than none: it looks like the problem was addressed.
3. **Refuse to overwrite a still-valid remote credential with a dead
   one**, and refuse outright to send anything carrying refresh material
   — cloning refresh material is the defect this command prevents.
4. **Back up what is replaced**, then publish atomically at 0600 (or, on
   a bind-mounted destination where rename is impossible, deliberately in
   place — see :mod:`._snapshot_publish`).
5. **Verify the FAR SIDE returns HTTP 200 before restarting anything.**
   A round that skipped this restarted agents onto an unverified token
   and was wasted.
6. **Only then** sweep the peer's 401ing agents — and only when asked.

CONVERGENT, NOT TICK-DRIVEN
---------------------------
Measured the same day: invoking ``claude -p`` on the master did NOT
rotate anything — the access fingerprint and expiry were identical before
and after, because Claude Code refreshes only when the token is genuinely
near expiry. So a job that fires on a clock and pushes unconditionally is
a no-op for most of a ~7h token's life and does the real work only in the
run that happens to straddle the refresh. This module instead compares
FINGERPRINTS and pushes when they differ, which converges after any
refresh however it was triggered. The schedule then only bounds the
post-refresh gap; it does not decide what the work is.

SECRECY CONTRACT (HARD)
-----------------------
No token value is ever printed, logged, returned or written to a local
file. Every record this module produces carries paths, hostnames,
expiry-in-seconds and opaque ``sha256:`` fingerprints — nothing else.
The access token exists here only as in-memory bytes that travel once,
over the transport's stdin, into the peer's ``dd``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ._keepalive_guards import (
    MIN_VALIDITY_S,
    KeepaliveError,
    assert_access_only,
    assert_is_refresh_holder,
    assert_not_downgrading,
    build_payload,
    find_refresh_keys,
    holds_refresh_material,
    refresh_holder_accounts,
    seconds_left,
)
from ._keepalive_remote import (
    backup_remote,
    ensure_remote_dir,
    install_probe,
    read_remote_state,
    remove_probe,
    verify_remote_token,
)
from .snapshot_push import PeerTransport, push_snapshot

#: The one status that licenses a restart on the far side.
VERIFY_OK = 200

# What a NON-LOGIN remote shell says when the command is not on its PATH.
# Measured 2026-08-10: `ssh host 'claude ...'` answered "No such file or
# directory" — not a credential failure, a PATH failure, because sshd's
# non-login shell never sources the profile block that puts the fleet's
# tools on PATH. Any remote invocation of a NON-coreutils command has to
# reckon with this.
_NOT_FOUND_MARKERS = ("command not found", "no such file or directory")
_NOT_FOUND_CODES = (126, 127)


def keepalive_push(
    account: str,
    peer: str,
    *,
    transport: PeerTransport | None = None,
    min_validity_s: int = MIN_VALIDITY_S,
    remote_path: str | None = None,
    force: bool = False,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
    verify_url: str | None = None,
) -> dict[str, Any]:
    """Converge ``peer`` onto the master's access token and PROVE it works.

    Runs the ordering documented at the top of this module. Every failure
    raises, naming the account and the peer — there is no silent-success
    path, because a silent failure is exactly how the 2026-08-10 outage
    stayed invisible for two hours.

    When the peer's fingerprint ALREADY matches the master's, nothing is
    published (no write, no backup) but the far side is still verified —
    so a run that has no work to do still answers "is this peer able to
    authenticate", which is the question that matters. ``force`` publishes
    regardless.

    Args:
        account: the stored-account slug on the MASTER (the only host that
            holds refresh material).
        peer: a peer key from ``~/.scitex/agent-container/config.yaml``.
        transport: the transfer seam; ``None`` builds the real ssh
            transport from sac's peer table.
        min_validity_s: refuse when the master token has less than this
            many seconds left.
        remote_path: override the destination. Defaults to the IDENTICAL
            absolute path the snapshot occupies on the master.
        force: publish even when the peer already holds this exact token.
        store_dir, home, now: test seams.
        verify_url: the endpoint the peer must answer 200 on. ``None`` uses
            the real API. A seam so a test can point the REAL probe at a
            REAL local server instead of faking the one step whose entire
            purpose is to make a real request.

    Returns:
        A record of paths, fingerprints and expiries — never a token. Its
        ``action`` is ``"pushed"`` or ``"already-current"``::

            {"account", "peer", "action", "remote_path", "backup_path",
             "mode", "bytes", "publish", "expires_at_ms", "seconds_left",
             "access_fp", "previous_access_fp", "previous_seconds_left",
             "peer_held_refresh_material", "verify_status"}

    Raises:
        KeepaliveError: any refusal, or a far side that does not answer 200.
        SnapshotPushError: a transport / verification failure on the peer.
    """
    now_s = now if now is not None else time.time()
    assert_is_refresh_holder(account, peer=peer, store_dir=store_dir, home=home)
    payload = build_payload(
        account,
        peer=peer,
        min_validity_s=min_validity_s,
        store_dir=store_dir,
        home=home,
        now=now_s,
    )

    from .._state.account_store import _store_path

    local_home = home if home is not None else Path.home()
    local_path = _store_path(store_dir, local_home) / account / ".credentials.json"
    remote = remote_path if remote_path is not None else str(local_path)

    if transport is None:
        from .snapshot_push import resolve_peer_transport

        transport = resolve_peer_transport(peer)

    ensure_remote_dir(transport, remote)
    probe = install_probe(transport, remote, verify_url=verify_url)
    try:
        state = read_remote_state(transport, probe, remote)
        current = (
            not state["absent"]
            and state["access_fp"] is not None
            and state["access_fp"] == payload["access_fp"]
        )

        backup: str | None = None
        published: dict[str, Any] = {
            "remote_path": remote,
            "mode": None,
            "bytes": None,
            "publish": None,
        }
        if current and not force:
            action = "already-current"
        else:
            action = "pushed"
            assert_not_downgrading(
                state,
                account=account,
                peer=peer,
                expires_at_ms=payload["expires_at_ms"],
                now_s=now_s,
            )
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now_s))
            backup = backup_remote(transport, remote, stamp=stamp)
            published = push_snapshot(
                account,
                local_path,
                transport=transport,
                remote_path=remote,
                payload=payload["bytes"],
            )

        status = verify_remote_token(transport, probe, remote)
        if status != VERIFY_OK:
            raise KeepaliveError(
                _reject_message(account, peer, remote, status, action, backup)
            )
    finally:
        remove_probe(transport, remote)

    return {
        "account": account,
        "peer": peer,
        "action": action,
        "remote_path": published["remote_path"],
        "backup_path": backup,
        "mode": published["mode"],
        "bytes": published["bytes"],
        "publish": published["publish"],
        "expires_at_ms": payload["expires_at_ms"],
        "seconds_left": payload["seconds_left"],
        "access_fp": payload["access_fp"],
        "previous_access_fp": state["access_fp"],
        "previous_seconds_left": (
            None if state["absent"] else seconds_left(state["expires_at_ms"], now_s)
        ),
        "peer_held_refresh_material": bool(state["refresh_fp"]),
        "verify_status": status,
    }


def _reject_message(
    account: str,
    peer: str,
    remote: str,
    status: int,
    action: str,
    backup: str | None,
) -> str:
    """Diagnose a far side that refused the credential. Never a token."""
    if action == "already-current":
        # The peer holds EXACTLY the master's token and that token is being
        # refused — so the master's own token is dead, and no amount of
        # pushing will fix it. Say that, rather than reporting a peer fault.
        return (
            f"peer '{peer}' was ALREADY holding the master's exact token for "
            f"account '{account}' and it is REJECTED (HTTP {status}). Nothing "
            "was changed. This is a MASTER-side failure, not a peer one: the "
            "master's own access token has been revoked or expired. "
            "Re-authenticate on the refresh holder, then re-run."
        )
    return (
        f"peer '{peer}' REJECTED the credential just published at {remote} "
        f"(HTTP {status}). NOT restarting anything on '{peer}'. The previous "
        f"credential is preserved at "
        f"{backup or '(none — this was the first push)'}."
    )


def _looks_missing(returncode: int, stderr: str) -> bool:
    """True when a remote command failed because it was not on the PATH."""
    text = (stderr or "").lower()
    return returncode in _NOT_FOUND_CODES or any(
        marker in text for marker in _NOT_FOUND_MARKERS
    )


def sweep_login_expired(peer: str, *, run_fn: Callable[..., Any] | None = None) -> str:
    """Restart the peer's agents that are wedged on auth. Runs LAST, or never.

    A push alone does not revive a 401ing agent: a running ``claude`` holds
    its token in memory, so replacing the file on disk changes nothing
    until the process restarts. This is the sweep leg — the sac-native
    equivalent of the prototype's ``restart-401.sh``, dispatched as ``sac
    agents restart-login-expired --apply`` on the peer.

    It is OPT-IN (``--sweep``) and it runs strictly AFTER the far side has
    returned 200, never before. Restarting agents is a mutating action on
    another machine, and the peer may already run its own ``auth-heal``
    supervisor — two restarters on one fleet is the double-supervisor
    class (see ``cli_pkg/_agents_restart_login_expired.py``).

    THE NON-LOGIN-SHELL TRAP is handled explicitly. sshd hands the command
    to a NON-login shell, which does not source the profile block that puts
    ``sac`` on PATH; the first attempt therefore uses sac's normal remote
    argv (so the peer's ``env_preamble`` and the registry's ``SCITEX_DIR``
    pin still apply), and a not-found result is RETRIED once through
    ``bash -lc``. Which form succeeded is stated in the returned output —
    a silent fallback would hide a peer whose PATH needs fixing.

    Returns the peer's combined output. Raises :class:`KeepaliveError`
    naming the peer on a non-zero exit.
    """
    import shlex

    from .._state.host_config import build_ssh_argv
    from .._state.host_config import load as load_host_config

    config = load_host_config()
    if peer not in config.peers:
        known = ", ".join(sorted(config.peers)) or "(none registered)"
        raise KeepaliveError(
            f"cannot sweep peer '{peer}': not found under peers: in "
            f"{config.source_path}. Registered peers: {known}."
        )
    if run_fn is None:
        import subprocess

        run_fn = subprocess.run

    remote_cmd = ["sac", "agents", "restart-login-expired", "--apply"]
    argv = build_ssh_argv(peer, remote_cmd, config.peers)
    proc = run_fn(argv, capture_output=True, text=True, check=False)
    note = ""
    if proc.returncode != 0 and _looks_missing(proc.returncode, proc.stderr or ""):
        # PATH, not auth. Retry through a LOGIN shell, collapsed into ONE
        # argv element because ssh joins everything after the host with
        # spaces and the remote shell re-parses it.
        login = [f"bash -lc {shlex.quote(shlex.join(remote_cmd))}"]
        argv = build_ssh_argv(peer, login, config.peers)
        proc = run_fn(argv, capture_output=True, text=True, check=False)
        note = (
            f"[sweep] '{peer}' has no `sac` on its NON-login shell PATH; "
            "retried through `bash -lc`. Fix that host's PATH so the direct "
            "form works.\n"
        )

    output = f"{note}{proc.stdout or ''}{proc.stderr or ''}".strip()
    if proc.returncode != 0:
        raise KeepaliveError(
            f"the 401 sweep on peer '{peer}' failed (`sac agents "
            f"restart-login-expired --apply` exited {proc.returncode}). The "
            f"credential IS published and verified on '{peer}'; only the "
            f"restart of its wedged agents did not happen: {output}"
        )
    return output


__all__ = [
    "MIN_VALIDITY_S",
    "VERIFY_OK",
    "KeepaliveError",
    "assert_access_only",
    "assert_is_refresh_holder",
    "assert_not_downgrading",
    "build_payload",
    "find_refresh_keys",
    "holds_refresh_material",
    "keepalive_push",
    "refresh_holder_accounts",
    "seconds_left",
    "sweep_login_expired",
]
