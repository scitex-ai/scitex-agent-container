"""``sac accounts refresh`` — headless OAuth access-token rotation.

Split out of ``account_group.py`` to keep that orchestrator under the
per-file line cap; the command is attached onto the ``account`` group at
import time via :func:`register_refresh_command` (same pattern as
``_account_sync_live.register_sync_live_commands``).

As long as the (long-lived) refresh_token is still valid, the
access_token is rotated in place and atomically written back to the same
per-account credentials file — eliminating routine manual ``claude
/login`` for stored accounts.
"""

from __future__ import annotations

from pathlib import Path

import click

from ._account_refresh_gate import (
    iso_ms,
    needs_refresh,
    refusal_message,
)
from ._account_refresh_skip import (
    _collect_pinned_running_accounts,
    _resolve_active_account_name,
)


@click.command("refresh")
@click.argument("name", required=False)
@click.option(
    "--all",
    "do_all",
    is_flag=True,
    default=False,
    help="Refresh every stored account in turn (one network call each).",
)
@click.option(
    "--skip-active",
    "skip_active",
    is_flag=True,
    default=False,
    help=(
        "With --all, exclude the account matching the currently-active "
        "~/.claude login (avoids rotating the in-use refresh_token and "
        "breaking the live session)."
    ),
)
@click.option(
    "--include-active",
    "include_active",
    is_flag=True,
    default=False,
    help=(
        "With --all, refresh EVERY stored account INCLUDING the active "
        "and any pinned-running one (skip nothing). This is the mode the "
        "host-side sac-accounts-refresh timer runs under the master-host "
        "single-refresher model: agents bind the credential :ro and never "
        "refresh, so the timer must refresh the active account too or its "
        "agents die at token expiry. Mutually exclusive with --skip-active."
    ),
)
@click.option(
    "--min-ttl-hours",
    "min_ttl_hours",
    type=float,
    default=2.0,
    show_default=True,
    help=(
        "Refresh a snapshot ONLY when its access token has less than this "
        "many hours of life remaining (a token with unknown/absent expiry is "
        "always refreshed). Fresh tokens are left untouched — this is the "
        "rotate-only-when-stale gate, which avoids needlessly rotating a "
        "single-use refresh_token and stranding every agent holding the "
        "current access token. Applies to a single named account too; use "
        "--force to rotate a still-fresh one deliberately."
    ),
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help=(
        "Ignore --min-ttl-hours and refresh even a still-fresh token. "
        "Required to rotate a single named account before its TTL gate "
        "opens — rotation invalidates the access token every running agent "
        "pinned to that account is currently using."
    ),
)
@click.option(
    "--sync-active-login",
    "sync_active_login_flag",
    is_flag=True,
    default=False,
    help=(
        "After refreshing the account whose snapshot refresh_token matches "
        "the live ~/.claude login, ALSO write the freshly-rotated token block "
        "into ~/.claude/.credentials.json so the operator's live session is "
        "never stranded by the rotation (single-use refresh_token). The write "
        "is defended: backup -> atomic replace -> verify-or-restore. This is "
        "the flag the host-side refresher timer passes. Mutually exclusive "
        "with --skip-active."
    ),
)
@click.option(
    "--push-to",
    "push_to",
    default=None,
    metavar="PEER",
    help=(
        "After a SUCCESSFUL refresh, copy each freshly-rotated snapshot to "
        "PEER at the IDENTICAL absolute path, mode 0600 (read back off the "
        "peer and asserted). PEER is a key under peers: in "
        "~/.scitex/agent-container/config.yaml — the same table `sac host "
        "list` shows. OPT-IN: a peer is a DIFFERENT filesystem, so agents "
        "running there bind a copy of the snapshot that nothing on that box "
        "ever refreshes, and they silently 401 within one token lifetime. A "
        "failed push fails the run (non-zero exit) — never a silent success."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON array on stdout instead of human prose.",
)
def account_refresh(
    name: str | None,
    do_all: bool,
    skip_active: bool,
    include_active: bool,
    min_ttl_hours: float,
    force: bool,
    sync_active_login_flag: bool,
    push_to: str | None,
    as_json: bool,
) -> None:
    """Mint a fresh access_token from the stored refresh_token, headlessly.

    Eliminates routine manual `claude /login`: as long as the (long-lived)
    refresh_token is still valid, the access_token is rotated in place,
    atomically written back to the same per-account credentials file.

    A real `claude /login` is only required when the refresh_token itself
    has lapsed — in which case this command reports it per-account and
    moves on (with ``--all``) rather than aborting the whole run.

    ``--skip-active`` (with ``--all``) excludes the account that matches
    the currently-active ``~/.claude`` login, so the refresh job never
    rotates the in-use refresh_token out from under the live session.

    ``--include-active`` (with ``--all``) is the opposite intent, made
    explicit: refresh EVERY account including the active + pinned-running
    ones (skip nothing). Under the master-host single-refresher model
    (2026-07-08) agents bind the credential ``:ro`` and never refresh, so
    the host-side ``sac-accounts-refresh`` timer is the SOLE refresher and
    MUST refresh the active account too — otherwise the active account's
    agents die when its access_token expires. The timer's ExecStart uses
    this flag. Mutually exclusive with ``--skip-active``.

    ``--min-ttl-hours`` makes the refresh a rotate-only-when-stale gate: an
    account whose snapshot access token still has more than the threshold
    left is skipped (no network call, no refresh_token rotation), a token
    with unknown/absent expiry is always refreshed. ``--force`` bypasses
    the gate.

    The gate applies to a SINGLE NAMED ACCOUNT exactly as it does under
    ``--all`` (INCIDENT 2026-08-09). It used to be ignored there, on the
    reasoning that an explicit request should always refresh — but a
    refresh is not a read: it rotates the single-use refresh_token, which
    invalidates the access token EVERY running agent pinned to that
    account is holding, on every host that binds the snapshot. On
    2026-08-09 one `sac accounts refresh <name>`, run as a diagnostic on
    the master, stranded a whole host's agents with 401s while the timer's
    ``--all`` path had been correctly skipping that same account as still
    fresh. The safe default belonged on both paths, and the DEBUGGING path
    is the one a human reaches for under pressure. A named account whose
    token is still fresh is now REFUSED with exit code 2 and a message
    naming what the rotation would strand; ``--force`` is the way past.

    ``--sync-active-login`` (with --all) additionally keeps the operator's
    live ``~/.claude/.credentials.json`` in sync: before refreshing, the
    account whose snapshot refresh_token EQUALS the live login's is
    identified (equality only — the value is never printed); when THAT
    account is refreshed, its freshly-rotated token block is also written
    into the live file (backup -> atomic replace -> verify-or-restore) so a
    single-use refresh_token rotation never strands the live session.

    ``--push-to PEER`` (opt-in) closes the SAME staleness hole for agents
    on a REMOTE peer. The single-refresher model above only reaches agents
    that share this machine's filesystem; a peer (Spartan) is a different
    box, and its own copy of the snapshot is refreshed by nothing, so its
    agents silently 401 within one access-token lifetime. With this flag,
    each snapshot that ACTUALLY rotated in this run (skipped-fresh and
    failed accounts are never pushed) is copied to the peer's IDENTICAL
    absolute path. The file must land mode 0600, and the mode is READ BACK
    off the peer and asserted — an OAuth token must never be world-readable
    on a shared HPC filesystem. Nothing is published before it verifies,
    and a failed push fails the run (non-zero exit): a silent push failure
    would recreate exactly the invisible staleness the flag exists to kill.
    PEER is resolved through sac's existing peer table (``sac host list``);
    an unknown peer is rejected BEFORE any refresh, so a typo never costs a
    single-use refresh_token rotation.

    \b
    Examples:
      $ sac accounts refresh work
      $ sac accounts refresh --all
      $ sac accounts refresh --all --skip-active
      $ sac accounts refresh --all --include-active   # legacy timer mode
      $ sac accounts refresh --all --sync-active-login  # daemon mode
      $ sac accounts refresh --all --push-to spartan    # keep a peer fresh
      $ sac accounts refresh --all --json
    """
    import json as _json

    from .._account._rotation_audit import fingerprint_token, log_rotation_event
    from .._account.active_login_write import (
        ActiveLoginSyncError,
        read_refresh_token,
        sync_active_login,
    )
    from .._account.claude_usage import _read_tokens_at, refresh_account_credentials
    from .._state.account_store import _store_path, list_accounts

    if not do_all and not name:
        click.echo(
            "error: provide an account name or --all "
            "(see `sac accounts refresh --help`)",
            err=True,
        )
        raise SystemExit(2)
    if do_all and name:
        click.echo("error: pass either a name or --all, not both", err=True)
        raise SystemExit(2)
    if skip_active and include_active:
        click.echo(
            "error: --skip-active and --include-active are mutually "
            "exclusive (one skips the active account, the other forces it in)",
            err=True,
        )
        raise SystemExit(2)
    if sync_active_login_flag and skip_active:
        click.echo(
            "error: --sync-active-login and --skip-active are mutually "
            "exclusive (syncing the live login requires refreshing the active "
            "account, which --skip-active excludes)",
            err=True,
        )
        raise SystemExit(2)

    # Resolve --push-to BEFORE any refresh runs. A refresh CONSUMES the
    # single-use OAuth refresh_token, so discovering a typo'd peer name
    # afterwards would have cost a rotation for nothing.
    push_transport = None
    if push_to:
        from .._account.snapshot_push import UnknownPeerError, resolve_peer_transport

        try:
            push_transport = resolve_peer_transport(push_to)
        except UnknownPeerError as exc:
            click.echo(f"error: {exc}", err=True)
            raise SystemExit(2) from exc

    home = Path.home()
    store = _store_path(None, home)

    if do_all:
        accounts = list_accounts(home=home)
        targets = [a["name"] for a in accounts]
        if include_active:
            # Master-host single-refresher: refresh everything, skip
            # nothing. Log it so the operator can see the timer's intent.
            click.echo(
                "[include-active] refreshing ALL accounts including the "
                "active + pinned-running ones (single-refresher model).",
                err=True,
            )
        if skip_active:
            active_name = _resolve_active_account_name(home, accounts)
            pinned_running = _collect_pinned_running_accounts(home)
            if active_name is None:
                click.echo(
                    "[skip-active] no active account resolvable; "
                    "host-active skip is a no-op.",
                    err=True,
                )
            elif active_name in targets:
                click.echo(
                    f"[skip-active] excluding active account '{active_name}'.",
                    err=True,
                )
            for pinned_name in sorted(pinned_running & set(targets)):
                click.echo(
                    f"[skip-active] excluding pinned-running account "
                    f"'{pinned_name}' (refresh-token rotation race guard).",
                    err=True,
                )
            skip_set: set[str] = set(pinned_running)
            if active_name:
                skip_set.add(active_name)
            targets = [t for t in targets if t not in skip_set]
    else:
        targets = [name]  # type: ignore[list-item]

    # Active-login family detection (for --sync-active-login). Read the live
    # ~/.claude login's refresh_token ONCE (realpath, symlinks followed) and
    # find the target account whose snapshot refresh_token EQUALS it —
    # equality only, the value is never printed. That is the SOLE account
    # whose rotation may be mirrored into the live login (never cross-account).
    live_path = (home / ".claude" / ".credentials.json").resolve()
    active_family: str | None = None
    if sync_active_login_flag:
        live_refresh = read_refresh_token(live_path)
        if live_refresh:
            for t in targets:
                snap_refresh = read_refresh_token(store / t / ".credentials.json")
                if snap_refresh is not None and snap_refresh == live_refresh:
                    active_family = t
                    break
        if active_family is not None:
            click.echo(
                f"[sync-active-login] live ~/.claude login matches account "
                f"'{active_family}'; its rotation will be mirrored into the "
                "live login.",
                err=True,
            )
        else:
            click.echo(
                "[sync-active-login] no stored account matches the live "
                "~/.claude login (or no live login); nothing to mirror.",
                err=True,
            )

    def _needs_refresh(expires_ms: int | None) -> bool:
        return needs_refresh(
            expires_ms, force=force, min_ttl_hours=min_ttl_hours
        )

    results: list[dict] = []
    sync_failed = False
    for acct_name in targets:
        creds_path = store / acct_name / ".credentials.json"
        # OUTGOING token fingerprint + expiry (before the refresh rotates it).
        old_access, old_refresh, _, old_expires = _read_tokens_at(creds_path)

        # Rotate-only-when-stale gate: leave a still-fresh snapshot untouched
        # so a single-use refresh_token is never needlessly rotated.
        if not _needs_refresh(old_expires):
            results.append(
                {
                    "name": acct_name,
                    "success": None,
                    "skipped": True,
                    "expires_at": iso_ms(old_expires),
                    "error": None,
                    "credentials_path": str(creds_path),
                }
            )
            continue

        # stx-allow: fallback (reason: refresh_account_credentials is documented never-raise, but defence-in-depth so one bad row never crashes --all)
        try:
            r = refresh_account_credentials(creds_path)
        except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            r = {
                "success": False,
                "expires_at": None,
                "error": f"unexpected error: {exc}",
                "credentials_path": str(creds_path),
            }
        r["name"] = acct_name
        r["skipped"] = False
        results.append(r)

        # Rotation audit: a successful refresh IS a single-use refresh_token
        # rotation — THE key "mystery expiry" event. Best-effort, never fails
        # the refresh run. Only opaque fingerprints are recorded.
        if r.get("success"):
            new_access, new_refresh, _, _ = _read_tokens_at(creds_path)
            log_rotation_event(
                store=store,
                event="refresh",
                from_account=acct_name,
                to_account=acct_name,
                reason="single-use refresh_token rotated (headless access-token refresh)",
                from_token_fp=fingerprint_token(old_access),
                to_token_fp=fingerprint_token(new_access),
                refresh_token_fp=fingerprint_token(new_refresh or old_refresh),
            )

            # Active-login mirror: ONLY the matched active-family account, and
            # ONLY after its snapshot rotated. The freshly-minted token block
            # is copied into the live ~/.claude login under the
            # backup -> atomic replace -> verify-or-restore contract. A
            # verification failure restores the original and fails the run.
            if sync_active_login_flag and acct_name == active_family:
                # stx-allow: fallback (reason: ActiveLoginSyncError is the ONLY expected failure — it has already restored the live file from .bak; we record it, fail the run loud, and never crash mid-loop.)
                try:
                    sync_active_login(live_path, creds_path)
                    r["synced_live_login"] = True
                    click.echo(
                        f"  [sync-active-login] mirrored '{acct_name}' rotation "
                        "into the live ~/.claude login.",
                        err=True,
                    )
                except ActiveLoginSyncError as exc:  # stx-allow: fallback (reason: see inline comment)
                    sync_failed = True
                    r["synced_live_login"] = False
                    r["sync_error"] = str(exc)
                    click.echo(
                        f"  [sync-active-login] FAILED for '{acct_name}': {exc}",
                        err=True,
                    )

    # Peer push (opt-in). Runs AFTER the refresh loop so it carries only
    # the snapshots that actually rotated, and BEFORE the results are
    # rendered so --json and the human table both report the push outcome.
    push_failed = False
    if push_to:
        from ._account_refresh_push import push_refreshed_snapshots

        push_failed = push_refreshed_snapshots(
            results, push_to, transport=push_transport
        )

    if as_json:
        click.echo(_json.dumps(results, ensure_ascii=False, indent=2))
    else:
        if not results:
            click.echo("No accounts stored. Use: sac accounts save <name>")
        for r in results:
            if r.get("skipped"):
                click.echo(
                    f"  {r['name']:20s}  skipped; token still fresh "
                    f"(TTL >= {min_ttl_hours:g}h)"
                )
            elif r["success"]:
                click.echo(
                    f"  {r['name']:20s}  refreshed; new expiry "
                    f"{r['expires_at'] or '(unknown)'}"
                )
            else:
                click.echo(f"  {r['name']:20s}  FAILED — {r['error']}", err=True)

    # LOUD failure alerting (INCIDENT 2026-07-10): a failed refresh —
    # most importantly a rejected/unreachable refresh grant — pushes an
    # immediate typed ``blocker`` to the lead via the existing ADR-0013
    # rail, deduped per account until that account refreshes OK again.
    # Runs for EVERY invocation shape (timer --all, manual single name).
    # Never raises; alert lines go to stderr under the results table.
    from .._account.refresh_alarm import alert_failed_refreshes

    alert_failed_refreshes(results)

    # Exit non-zero when EVERY *attempted* account failed (skipped-fresh
    # accounts don't count), when an active-login sync failed loud, OR when
    # a --push-to peer push failed. A push that failed silently would leave
    # the peer's agents running on a snapshot nothing refreshes — the exact
    # invisible-staleness bug the flag exists to kill — so it is loud.
    # --all with mixed results is still a useful partial success.
    # A NAMED account held back by the TTL gate is a REFUSAL TO ACT, not a
    # quiet no-op: the caller asked for a rotation and got none, so saying
    # nothing would read as success. Exit 2 — the remedy is a flag, not a
    # retry, which is this file's existing meaning for 2 (usage), while 1
    # stays "the refresh was attempted and failed".
    if not do_all and results and results[0].get("skipped"):
        refused = results[0]
        click.echo(
            refusal_message(
                refused["name"],
                refused.get("expires_at"),
                min_ttl_hours,
                is_pinned=refused["name"] in _collect_pinned_running_accounts(home),
            ),
            err=True,
        )
        raise SystemExit(2)

    attempted = [r for r in results if not r.get("skipped")]
    all_attempted_failed = bool(attempted) and not any(
        r.get("success") for r in attempted
    )
    if all_attempted_failed or sync_failed or push_failed:
        raise SystemExit(1)


def register_refresh_command(group: click.Group) -> None:
    """Attach the ``refresh`` command onto the ``account`` group."""
    group.add_command(account_refresh)


__all__ = ["account_refresh", "register_refresh_command"]
