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


def format_peer_lines(peers: "list[str]") -> list[str]:
    """Render ``peers`` as the ``--help`` block listing what ``--to`` takes.

    Pure, so the rendering can be asserted without a host's real
    ``config.yaml`` deciding the expected output — a test that reads the
    live table would assert an environmental fact rather than a property
    of this function.
    """
    if not peers:
        return []
    lines = ["Registered peers --to accepts on THIS host:"]
    for name in sorted(peers):
        # A wildcard row (``spartan-*``) is a TEMPLATE for per-node keys,
        # not a name anyone can type. Printing it bare would repeat the
        # defect this listing exists to fix: help that names something
        # the command rejects.
        if "*" in name:
            lines.append(f"  {name}  (pattern — substitute a real node name)")
        else:
            lines.append(f"  {name}")
    return lines


def _registered_peer_lines() -> list[str]:
    """The peer keys ``--to`` will actually accept, for ``--help``.

    ``--to`` is documented as "a peer key from config.yaml", and the
    shipped examples named ``compute-04`` and ``laptop`` — neither of
    which is a key on any host. Copy-pasting the documentation therefore
    failed. Reading the real table at render time means the help cannot
    drift from what the command accepts.

    Returns ``[]`` on ANY failure. ``--help`` must render on a machine
    with no config, an unreadable config or a malformed one; a help
    screen that raises is worse than a help screen that omits a hint.
    """
    try:
        from .._state.host_config import load as load_host_config

        peers = list(load_host_config().peers)
    except Exception:  # stx-allow: fallback (reason: --help must never raise; an unreadable/absent config yields no hint rather than a crash)
        return []
    return format_peer_lines(peers)


class _PeerListingCommand(click.Command):
    """A command whose ``--help`` ends with the peer keys ``--to`` takes."""

    def format_epilog(self, ctx, formatter) -> None:
        super().format_epilog(ctx, formatter)
        lines = _registered_peer_lines()
        if not lines:
            return
        formatter.write_paragraph()
        with formatter.indentation():
            for line in lines:
                formatter.write_text(line)


def register_keepalive_command(group: click.Group) -> None:
    """Attach ``send-credentials`` (and its ``keepalive`` alias) onto ``group``."""

    @click.command("send-credentials", cls=_PeerListingCommand)
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
            "peer key from ~/.scitex/agent-container/config.yaml — this "
            "--help lists the ones registered here, and `sac host list` "
            "shows them with their ssh targets."
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
        "--optional-peer",
        "optional_peers",
        multiple=True,
        metavar="PEER",
        help=(
            "Declare a peer as INTERMITTENT: it is still pushed to, and any "
            "failure is still printed, but its failure does not fail the run. "
            "For laptops and anything else that is legitimately off. Must "
            "also appear in --to. Repeatable. Nothing is implicit here — a "
            "peer is optional only because the command line says so, so the "
            "unit file states exactly which hosts may be absent."
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
        optional_peers: tuple[str, ...],
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
        Examples (peer keys below are real; run --help to see THIS host's):
          $ sac accounts send-credentials --account alpha-example-com --to scitex-compute-04
          $ sac accounts send-credentials --all --to scitex-compute-04 --to ywata-note-win
          $ sac accounts send-credentials --all --to scitex-compute-04 --sweep
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
        from . import _account_keepalive_pause as _kp

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

        # An optional peer must be one we are actually pushing to. Silently
        # accepting a name that is not in --to would let a typo disarm
        # nothing while looking like it disarmed something.
        optional = set(optional_peers)
        unknown_optional = sorted(optional - set(peers))
        if unknown_optional:
            raise click.UsageError(
                "--optional-peer names host(s) absent from --to: "
                f"{', '.join(unknown_optional)}. An optional peer must also "
                "be a target, or the declaration applies to nothing."
            )

        floor = MIN_VALIDITY_S if min_validity is None else min_validity
        records: list[dict[str, Any]] = []
        verified_peers: list[str] = []
        failed = False
        tolerated: list[str] = []

        # A PAUSED account is a DECISION, not a failure. The partition
        # happens HERE — before the loop, before any mint, before any ssh —
        # so a paused account can never reach `failed` below. That is the
        # whole mechanism: no new boolean, no second tolerance list, and the
        # exit at the end of this callback is untouched. See
        # :mod:`._account_keepalive_pause` for why this is a skip rather
        # than a tolerated failure, and why there is no --paused-account
        # flag.
        accounts, skipped = _kp.partition_paused(accounts)
        for account_label, pause in skipped:
            click.echo(_kp.skip_line(account_label, pause), err=True)
            records.append(_kp.skip_record(account_label, pause))

        # Empty-because-PAUSED exits 0. Empty-because-this-host-is-not-the-
        # origin still exits 1, at the guard above. The ordering is the
        # point: collapsing the two would re-create the always-red bug in
        # the one case where the operator has paused everything.
        if not accounts and skipped:
            click.echo(_kp.all_paused_line(len(skipped)), err=True)
            if as_json:
                click.echo(  # stx-allow: STX-IO006
                    _json.dumps(records, ensure_ascii=False, indent=2)
                )
            return

        for account_label in accounts:
            for peer in peers:
                # Name the attempt BEFORE it runs: a run killed mid-push
                # (the 2026-08-15 failure exited non-zero with ZERO journal
                # output) must still leave a line saying WHICH peer/account
                # it was on, or the silence returns. The outcome line below
                # follows; it never replaces this one.
                click.echo(
                    f"  {peer:20s}  {account_label}: pushing "
                    "access-only credential...",
                    err=True,
                )
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
                    is_optional = peer in optional
                    if is_optional:
                        tolerated.append(f"{peer}/{account_label}")
                    else:
                        failed = True
                    records.append(
                        {
                            "account": account_label,
                            "peer": peer,
                            "ok": False,
                            "error": str(exc),
                            "optional": is_optional,
                        }
                    )
                    label = "FAILED (optional peer)" if is_optional else "FAILED"
                    click.echo(
                        f"  {peer:20s}  {account_label}: {label} — {exc}", err=True
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
            # Same pre-attempt naming as the push loop: a run killed between
            # the verified push and the sweep restart would otherwise die
            # without saying it was sweeping THIS peer.
            click.echo(
                f"  {peer:20s}  sweeping login-expired agents...", err=True
            )
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

        # A tolerated failure must never be a silent one. The whole point of
        # declaring a peer optional is to keep the RED meaningful for the
        # always-on hosts; that only works if the run still says out loud
        # what it forgave, and how many.
        if tolerated:
            click.echo(
                f"  TOLERATED: {len(tolerated)} failure(s) on declared "
                f"optional peer(s) — {', '.join(tolerated)}. Exit stays 0 for "
                "these; they were declared intermittent with --optional-peer.",
                err=True,
            )

        if as_json:
            click.echo(  # stx-allow: STX-IO006
                _json.dumps(records, ensure_ascii=False, indent=2)
            )

        if failed:
            sys.exit(1)

    group.add_command(account_keepalive, "send-credentials")

    # ``keepalive`` is a PUBLISHED contract, so this is a migration, not a
    # rename: the old name keeps WORKING and is merely hidden from --help.
    # A redirect that printed "renamed to X" and exited would break the
    # systemd units and operator muscle memory that already call it, which
    # is the opposite of what a rename is for. Remove the alias only once
    # nothing on any host invokes it.
    legacy = click.Command(
        name="keepalive",
        callback=_deprecated(account_keepalive.callback),
        params=list(account_keepalive.params),
        help=account_keepalive.help,
        short_help="Deprecated alias for send-credentials.",
        epilog=account_keepalive.epilog,
        hidden=True,
    )
    group.add_command(legacy, "keepalive")


def _deprecated(callback):
    """Wrap ``callback`` so the legacy name says so — on stderr, then runs.

    stderr, not stdout: ``--json`` consumers parse stdout, and a warning
    that lands there would turn a working script into a JSON parse error.
    That is the whole failure this alias exists to prevent.
    """
    import functools

    @functools.wraps(callback)
    def _run(*args, **kwargs):
        click.echo(
            "warning: `sac accounts keepalive` is now "
            "`sac accounts send-credentials` — it copies credentials to "
            "peers, which is what the new name says. The old name still "
            "works and will be removed once nothing calls it.",
            err=True,
        )
        return callback(*args, **kwargs)

    return _run


__all__ = ["format_peer_lines", "register_keepalive_command"]
