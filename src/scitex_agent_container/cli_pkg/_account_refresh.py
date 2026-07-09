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
        "With --all, refresh a snapshot ONLY when its access token has less "
        "than this many hours of life remaining (a token with unknown/absent "
        "expiry is always refreshed). Fresh tokens are left untouched — this "
        "is the daemon's rotate-only-when-stale gate, which also avoids "
        "needlessly rotating a single-use refresh_token. Ignored for a "
        "single named account (an explicit request always refreshes)."
    ),
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="With --all, ignore --min-ttl-hours and refresh every account.",
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

    ``--min-ttl-hours`` (with --all) makes the refresh a rotate-only-when-
    stale gate: an account whose snapshot access token still has more than
    the threshold left is skipped (no network call, no refresh_token
    rotation), a token with unknown/absent expiry is always refreshed. A
    single named account ignores the gate (an explicit request always
    refreshes). ``--force`` bypasses the gate under --all.

    ``--sync-active-login`` (with --all) additionally keeps the operator's
    live ``~/.claude/.credentials.json`` in sync: before refreshing, the
    account whose snapshot refresh_token EQUALS the live login's is
    identified (equality only — the value is never printed); when THAT
    account is refreshed, its freshly-rotated token block is also written
    into the live file (backup -> atomic replace -> verify-or-restore) so a
    single-use refresh_token rotation never strands the live session.

    \b
    Examples:
      $ sac accounts refresh work
      $ sac accounts refresh --all
      $ sac accounts refresh --all --skip-active
      $ sac accounts refresh --all --include-active   # legacy timer mode
      $ sac accounts refresh --all --sync-active-login  # daemon mode
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

    import time as _time
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    def _needs_refresh(expires_ms: int | None) -> bool:
        """Rotate-only-when-stale gate. A single named account or --force
        always refreshes; under --all a token with more than
        ``min_ttl_hours`` left is left untouched (unknown expiry -> refresh)."""
        if force or not do_all:
            return True
        if expires_ms is None:
            return True
        hours_left = (expires_ms / 1000.0 - _time.time()) / 3600.0
        return hours_left < min_ttl_hours

    def _iso_ms(expires_ms: int | None) -> str | None:
        if not isinstance(expires_ms, int):
            return None
        return _dt.fromtimestamp(expires_ms / 1000, tz=_tz).isoformat()

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
                    "expires_at": _iso_ms(old_expires),
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

    # Exit non-zero when EVERY *attempted* account failed (skipped-fresh
    # accounts don't count), OR when an active-login sync failed loud.
    # --all with mixed results is still a useful partial success.
    attempted = [r for r in results if not r.get("skipped")]
    all_attempted_failed = bool(attempted) and not any(
        r.get("success") for r in attempted
    )
    if all_attempted_failed or sync_failed:
        raise SystemExit(1)


def register_refresh_command(group: click.Group) -> None:
    """Attach the ``refresh`` command onto the ``account`` group."""
    group.add_command(account_refresh)


__all__ = ["account_refresh", "register_refresh_command"]
