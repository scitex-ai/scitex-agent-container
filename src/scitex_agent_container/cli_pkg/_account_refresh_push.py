"""``sac accounts refresh --push-to <peer>`` — the CLI leg of the peer push.

Split out of :mod:`._account_refresh` to keep that command under the
per-file line cap (same pattern as ``_account_refresh_skip``).

The engine — the transfer, the 0600 verification and the fail-loud
contract — lives in :mod:`.._account.snapshot_push`. This module owns only
the CLI-shaped concerns:

* WHICH accounts get pushed (:func:`refreshed_accounts` — only the ones
  that actually rotated in this run),
* rendering each outcome on stderr, alongside the refresh table,
* stamping the per-account result dicts so ``--json`` carries the push
  outcome,
* and returning the single boolean the command folds into its exit code.

Why opt-in, and why it fails the run: a peer is a DIFFERENT filesystem.
An agent running there binds the peer's OWN copy of the snapshot, which
nothing on that box ever refreshes — it silently 401s within one token
lifetime. A push that failed but reported success would recreate exactly
that invisible staleness, so a failed push is a non-zero exit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

import click

from .._account.snapshot_push import (
    PeerTransport,
    SnapshotPushError,
    push_snapshot,
    resolve_peer_transport,
)


def refreshed_accounts(
    results: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the accounts whose snapshot ACTUALLY rotated in this run.

    Only these are pushed. A skipped-still-fresh account's snapshot is
    byte-identical to the one a previous run already pushed, and a FAILED
    refresh must never overwrite the peer's copy. The push therefore
    inherits the refresh's own rotate-only-when-stale discipline: it
    carries exactly the bytes that changed.
    """
    return [
        r
        for r in results
        if r.get("success") and not r.get("skipped") and r.get("credentials_path")
    ]


def push_refreshed_snapshots(
    results: Sequence[dict[str, Any]],
    peer: str,
    *,
    transport: PeerTransport | None = None,
    err: Callable[..., None] = click.echo,
) -> bool:
    """Push every snapshot refreshed in this run to ``peer``.

    Each result dict is stamped in place with ``pushed_to`` / ``push_error``
    so the command's ``--json`` output carries the outcome. Progress and
    failures are rendered on stderr under the refresh table.

    Args:
        results: the per-account result list ``sac accounts refresh`` built.
        peer: the peer key, already validated against sac's peer config by
            the caller (see :func:`.._account.snapshot_push.resolve_peer_transport`).
        transport: the transfer seam. ``None`` builds the real ssh
            transport from sac's peer config.
        err: sink for the human-facing lines (stderr by default).

    Returns:
        ``True`` iff at least one push FAILED — the command folds this into
        a non-zero exit. Never raises: every failure is captured, rendered
        and reported through the return value, so one dead peer cannot
        abort the loop before the other accounts are attempted.
    """
    targets = refreshed_accounts(results)
    if not targets:
        err(
            f"[push-to] no account rotated this run; nothing to push to "
            f"'{peer}'.",
            err=True,
        )
        return False

    active = transport if transport is not None else resolve_peer_transport(peer)
    failed = False

    for result in targets:
        account = str(result.get("name") or "?")
        local = Path(str(result["credentials_path"]))
        # stx-allow: fallback (reason: a push failure must be REPORTED for
        # this account and the loop must continue to the next one — one
        # unreachable peer path cannot silently drop the remaining
        # accounts. The failure is rendered loudly here and folded into a
        # non-zero exit via the returned flag; it is never swallowed.)
        try:
            record = push_snapshot(account, local, transport=active)
        except SnapshotPushError as exc:  # stx-allow: fallback (reason: see inline comment)
            failed = True
            result["pushed_to"] = None
            result["push_error"] = str(exc)
            err(
                f"  {account:20s}  PUSH FAILED -> {peer}:{local!s} — {exc}",
                err=True,
            )
            continue
        result["pushed_to"] = peer
        result["remote_path"] = record["remote_path"]
        err(
            f"  {account:20s}  pushed -> {peer}:{record['remote_path']} "
            f"(mode 0{record['mode']}, {record['bytes']} bytes, verified)",
            err=True,
        )

    return failed


__all__ = ["push_refreshed_snapshots", "refreshed_accounts"]
