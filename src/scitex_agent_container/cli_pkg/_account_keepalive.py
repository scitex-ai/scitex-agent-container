"""``sac accounts keepalive`` — push ACCESS-ONLY credentials out to peers.

The operator-facing leg of :mod:`.._account.token_keepalive`. Lives in its
own module (like ``_account_mint_token`` / ``_account_refresh``) to keep
``account_group`` under the per-file line cap; attached onto the group at
import time.

THE INVARIANT THIS MAINTAINS
----------------------------
Exactly ONE host in the fleet holds OAuth REFRESH material — as of
2026-08-10 that is ``scitex-nas-03``. Every other host is READ-ONLY with
respect to the session: it receives ACCESS-ONLY copies and can never
trigger the refresh-token rotation that would revoke everyone else's
token. ``sac accounts mint-token`` already mints that shape; this command
is what DELIVERS it, and what keeps the access-only hosts alive — without
a run before each expiry they simply go dark.

NOTHING IS SCHEDULED HERE. Running this on a timer is a separate,
deliberate decision and is expressly NOT hand-rolled as a unit file or a
cron line — those go missing. The recurrence belongs in scitex-dev's
periodic-job primitive, declared by sac. This module ships the verb only.
"""

from __future__ import annotations

from typing import Any

import click


def _render(record: dict[str, Any], out) -> None:
    """Render ONE successful push. Paths, fingerprints, seconds. No tokens."""
    peer = record["peer"]
    if record["action"] == "already-current":
        out(
            f"  {peer:20s}  {record['account']}: already current at "
            f"{record['remote_path']} — same token as the master, nothing "
            f"written (verified HTTP {record['verify_status']})",
            err=True,
        )
    else:
        out(
            f"  {peer:20s}  {record['account']}: pushed -> "
            f"{record['remote_path']} (mode 0{record['mode']}, "
            f"{record['bytes']} bytes, {record['publish']}, verified HTTP "
            f"{record['verify_status']})",
            err=True,
        )
    previous = record.get("previous_access_fp")
    replaced = (
        f"replaced {previous} ({record['previous_seconds_left']}s left)"
        if previous
        else "no previous credential on this peer"
    )
    out(
        f"  {'':20s}  token {record['access_fp']}, "
        f"{record['seconds_left']}s left; {replaced}",
        err=True,
    )
    if record.get("backup_path"):
        out(f"  {'':20s}  backup -> {record['backup_path']}", err=True)
    if record.get("publish") == "in-place":
        out(
            f"  {'':20s}  NOTE: the destination is bind-mounted, so the "
            "atomic rename was impossible (EBUSY) and the bytes were "
            "written IN PLACE. Non-atomic by necessity, not by choice.",
            err=True,
        )
    if record.get("peer_held_refresh_material"):
        out(
            f"  {'':20s}  WARNING: '{peer}' was holding REFRESH material "
            "before this push — that host was a CLONE of the master's OAuth "
            "session and could have revoked it. It now holds access-only "
            "material. Check for other clones with `sac accounts list`.",
            err=True,
        )


def register_keepalive_command(group: click.Group) -> None:
    """Attach the ``keepalive`` subcommand onto ``group``."""

    @group.command("keepalive")
    @click.option(
        "--account",
        "account_labels",
        multiple=True,
        help=(
            "Stored account slug to fan out (repeatable), e.g. "
            "alpha-example-com. Mutually exclusive with --all."
        ),
    )
    @click.option(
        "--all",
        "all_accounts",
        is_flag=True,
        default=False,
        help=(
            "Fan out EVERY stored account this host holds refresh material "
            "for — i.e. every account for which this host is the origin. "
            "Exits non-zero when that set is empty, because a host that is "
            "not the refresh holder cannot keep anyone alive and must say so "
            "rather than succeed quietly."
        ),
    )
    @click.option(
        "--to",
        "peers",
        multiple=True,
        required=True,
        help=(
            "Peer to push ACCESS-ONLY material to (repeatable). Must be a "
            "peer key from ~/.scitex/agent-container/config.yaml."
        ),
    )
    @click.option(
        "--min-validity",
        "min_validity",
        type=int,
        default=None,
        help=(
            "Refuse to push when the master token has fewer than this many "
            "seconds left (default 300). A token that expires in flight is "
            "worse than no push."
        ),
    )
    @click.option(
        "--remote-path",
        "remote_path",
        default=None,
        help=(
            "Override the destination path on every peer. Defaults to the "
            "IDENTICAL absolute path the snapshot occupies here."
        ),
    )
    @click.option(
        "--force",
        is_flag=True,
        default=False,
        help=(
            "Publish even when the peer already holds the master's exact "
            "token. Default is CONVERGENT: fingerprints are compared and a "
            "peer that is already current is verified but not rewritten, so "
            "a frequent schedule does not bury it in hourly backups."
        ),
    )
    @click.option(
        "--sweep",
        is_flag=True,
        default=False,
        help=(
            "After a peer VERIFIES the new credential, restart its agents "
            "that are wedged on auth (`sac agents restart-login-expired "
            "--apply` there). Off by default: a running claude holds its "
            "token in memory, so without this a 401'd agent stays 401'd — "
            "but the peer may already run its own auth-heal supervisor, and "
            "two restarters on one fleet fight each other."
        ),
    )
    @click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        help="Emit a JSON array of per-peer records on stdout.",
    )
    def account_keepalive(
        account_labels: tuple[str, ...],
        all_accounts: bool,
        peers: tuple[str, ...],
        min_validity: int | None,
        remote_path: str | None,
        force: bool,
        sweep: bool,
        as_json: bool,
    ) -> None:
        """Copy this host's CURRENT access token to peers, access-only.

        COPIES — it never mints or refreshes. Minting ROTATES the
        single-use refresh token, which revokes the access token every
        running agent is holding; that is what killed ten agents on
        2026-08-10. This reads the master's stored credential as it
        stands, strips the refreshToken (via the same
        ``mint_access_only_artifact`` the ``mint-token`` verb uses),
        refuses to send it if it is nearly dead, backs up whatever it
        replaces on the peer, publishes at 0600, and PROVES the peer's own
        copy answers HTTP 200 before anything is restarted.

        Every refusal is loud and names the account and the peer. No token
        value is ever printed — only paths, hostnames, seconds and opaque
        sha256 fingerprints.

        \b
        Examples:
          $ sac accounts keepalive --account alpha-example-com --to compute-04
          $ sac accounts keepalive --all --to compute-04 --to laptop
          $ sac accounts keepalive --all --to compute-04 --sweep
        """
        import json as _json
        import sys

        from .._account.snapshot_push import SnapshotPushError
        from .._account.token_keepalive import (
            MIN_VALIDITY_S,
            KeepaliveError,
            keepalive_push,
            refresh_holder_accounts,
            sweep_login_expired,
        )

        if bool(account_labels) == all_accounts:
            raise click.UsageError(
                "pass exactly one of --account <slug> (repeatable) or --all."
            )

        accounts = list(account_labels)
        if all_accounts:
            accounts = refresh_holder_accounts()
            if not accounts:
                click.echo(
                    "error: --all found no stored account this host holds "
                    "refresh material for, so this host is not the origin for "
                    "any session and cannot keep any peer alive. Run it on the "
                    "host that holds the refresh token.",
                    err=True,
                )
                sys.exit(1)
            click.echo(
                f"[keepalive] this host is the refresh holder for: "
                f"{', '.join(accounts)}",
                err=True,
            )

        floor = MIN_VALIDITY_S if min_validity is None else min_validity
        records: list[dict[str, Any]] = []
        verified_peers: list[str] = []
        failed = False

        for account_label in accounts:
            for peer in peers:
                # stx-allow: fallback (reason: one unreachable or refusing
                # peer must NOT abort the remaining peers — each host's
                # credential is independent, and aborting would leave the
                # untried hosts to expire silently, which is the exact
                # failure this command exists to end. Every failure is
                # rendered loudly here and folded into a non-zero exit
                # below; none is swallowed.)
                try:
                    record = keepalive_push(
                        account_label,
                        peer,
                        min_validity_s=floor,
                        remote_path=remote_path,
                        force=force,
                    )
                except (
                    KeepaliveError,
                    SnapshotPushError,
                ) as exc:  # stx-allow: fallback (reason: see inline comment)
                    failed = True
                    records.append(
                        {
                            "account": account_label,
                            "peer": peer,
                            "ok": False,
                            "error": str(exc),
                        }
                    )
                    click.echo(
                        f"  {peer:20s}  {account_label}: FAILED — {exc}", err=True
                    )
                    continue

                record["ok"] = True
                _render(record, click.echo)
                records.append(record)
                if peer not in verified_peers:
                    verified_peers.append(peer)

        # The sweep runs LAST and ONCE per peer — after every account has
        # been published AND verified there. Restarting between accounts
        # would bounce an agent onto a peer that is still mid-update.
        for peer in verified_peers:
            if not sweep:
                click.echo(
                    f"  {peer:20s}  not sweeping: agents there that are "
                    "already 401ing hold their old token in memory and will "
                    "stay wedged until restarted (pass --sweep).",
                    err=True,
                )
                continue
            # stx-allow: fallback (reason: the credential IS published and
            # verified at this point; a sweep failure must be reported
            # against THIS peer and must not discard the successful push or
            # skip the remaining peers.)
            try:
                output = sweep_login_expired(peer)
            except (
                KeepaliveError
            ) as exc:  # stx-allow: fallback (reason: see inline comment)
                failed = True
                records.append({"peer": peer, "ok": False, "sweep_error": str(exc)})
                click.echo(f"  {peer:20s}  SWEEP FAILED — {exc}", err=True)
                continue
            records.append({"peer": peer, "ok": True, "sweep_output": output})
            for line in str(output).splitlines():
                click.echo(f"    {line}", err=True)

        if as_json:
            click.echo(  # stx-allow: STX-IO006
                _json.dumps(records, ensure_ascii=False, indent=2)
            )

        if failed:
            sys.exit(1)


__all__ = ["register_keepalive_command"]
