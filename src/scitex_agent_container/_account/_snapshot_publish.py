"""Publish a VERIFIED staged credential onto its live path on a peer.

Split out of :mod:`.snapshot_push` (512-line cap) along a real seam: that
module owns staging + verification, this one owns the last two steps —
making the staged file live, and proving the live path is what was
verified.

WHY THIS IS NOT JUST ``mv``
---------------------------
The normal publish is an atomic same-directory rename, so an agent that
happens to open the snapshot mid-push can never read a torn file. But on
a host where the credential is BIND-MOUNTED into a container, rename onto
that path fails::

    OSError: [Errno 16] Device or resource busy

A rename cannot cross a bind mount — the destination is a mount point, not
an ordinary directory entry. Measured 2026-08-10 while stripping the
laptop's credential to access-only; the write had to be done in place.

Those are exactly the hosts this machinery exists for, so an EBUSY must
not be a failure and must not be swallowed either. It falls back to a
DELIBERATE in-place write, and the method actually taken (``"rename"`` or
``"in-place"``) is returned so every caller can say which one happened.
The in-place write is honestly worse — it is not atomic, so a concurrent
reader can observe a partial file for the duration of one small write —
and it is taken only when the atomic path is impossible.
"""

from __future__ import annotations

from .snapshot_push import (
    FILE_MODE,
    PeerTransport,
    RunResult,
    SnapshotPushError,
    _discard,
    _op,
    _tail,
    stat_remote,
)

#: Publish methods, in preference order.
RENAME = "rename"
IN_PLACE = "in-place"

# What a kernel / coreutils says when a rename hits a mount point. Matched
# case-insensitively against the remote `mv`'s stderr. Deliberately narrow:
# ANY other `mv` failure is a real failure and must stay loud.
_BUSY_MARKERS = (
    "device or resource busy",
    "resource busy",
    "text file busy",
    "ebusy",
)


def looks_busy(result: RunResult) -> bool:
    """True when a failed remote ``mv`` failed because the target is busy."""
    text = (result.stderr or "").lower()
    return any(marker in text for marker in _BUSY_MARKERS)


def publish_verified(
    account: str,
    *,
    transport: PeerTransport,
    staged: str,
    remote: str,
    payload: bytes,
) -> tuple[str, str, int]:
    """Make ``staged`` live at ``remote``, then prove the live path.

    Prefers the atomic same-directory rename. On EBUSY — a bind-mounted
    destination — falls back to writing the bytes in place, re-hardening
    to 0600 and discarding the staged file. Either way the LIVE path's
    mode and size are read back off the peer afterwards: ``mv`` preserves
    both, so a mismatch means the peer's filesystem mutated the file, and
    the in-place path must prove itself for the same reason the staged
    write did.

    Args:
        account: for error messages only.
        transport: the peer transfer seam.
        staged: the verified staging path (0600, correct size).
        remote: the live path to publish onto.
        payload: the same bytes already staged — re-sent only on the
            in-place fallback, over the transport's stdin as always.

    Returns:
        ``(method, mode, size)`` where ``method`` is :data:`RENAME` or
        :data:`IN_PLACE`. The caller is expected to REPORT the method: an
        in-place publish is a fact about that host worth seeing.

    Raises:
        SnapshotPushError: a non-EBUSY ``mv`` failure, a failed in-place
            write, or a live path that does not verify.
    """
    method = RENAME
    moved = transport.run(["mv", "-f", staged, remote])
    if moved.returncode != 0:
        if not looks_busy(moved):
            raise SnapshotPushError(
                f"failed to publish {remote} on peer '{transport.peer}' "
                f"(`mv` exited {moved.returncode})"
                f"{_tail(moved.stderr)}"
            )
        # EBUSY: the destination is a bind mount, so the atomic rename is
        # IMPOSSIBLE, not merely unlucky. Take the honest, non-atomic path
        # and say so — never silently.
        method = IN_PLACE
        _op(
            transport,
            ["dd", f"of={remote}"],
            what="write in place (bind-mounted, rename refused with EBUSY)",
            path=remote,
            stdin=payload,
        )
        _op(transport, ["chmod", FILE_MODE, remote], what="harden", path=remote)
        _discard(transport, staged)

    mode, remote_size = stat_remote(transport, remote)
    if mode != FILE_MODE or remote_size != len(payload):
        # A mode/size mismatch on the LIVE path means the peer's filesystem
        # mutated the file — hostile. Remove it rather than leave a token
        # whose mode cannot be vouched for. (An unreachable `stat` raises
        # from `stat_remote` above and deletes nothing: that inode was
        # already proven complete and 0600 before it was published.)
        _discard(transport, remote)
        raise SnapshotPushError(
            f"account '{account}' published to peer '{transport.peer}' at "
            f"{remote} via {method} did NOT verify (mode 0{mode}, "
            f"{remote_size} bytes; expected 0{FILE_MODE}, {len(payload)} "
            "bytes). The file has been removed from the peer rather than "
            "left unverified."
        )
    return method, mode, remote_size


__all__ = ["IN_PLACE", "RENAME", "looks_busy", "publish_verified"]
