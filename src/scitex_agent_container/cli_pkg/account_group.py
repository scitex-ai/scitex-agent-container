"""CLI commands for account and quota management.

Provides the ``account`` subcommand group (save/list/delete/switch/status)
and the top-level ``quota-watch`` command.
"""

from __future__ import annotations

import click

# ---------------------------------------------------------------------------
# account group
# ---------------------------------------------------------------------------


@click.group("account")
def account() -> None:
    """Inspect provider accounts and manage Claude credential rotation."""


# Credential auto-sync substrate (sync-live / watch-live) lives in its
# own module to keep this file under the per-file line cap; attach its
# commands onto the group at import time.
from ._account_sync_live import register_sync_live_commands

register_sync_live_commands(account)

from ._account_sync_openai import register_sync_openai_command

register_sync_openai_command(account)


@account.command("save")
@click.argument("name")
@click.option(
    "--email",
    default=None,
    help="Email address label for this account (informational only).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Show what would be saved without writing any files.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt (currently a no-op; reserved).",
)
def account_save(name: str, email: str | None, dry_run: bool, yes: bool) -> None:
    """Snapshot the current credentials under NAME for later rotation.

    \b
    Example:
      $ sac account save work
      $ sac account save work --email me@example.com
    """
    _ = yes  # accepted for API consistency; no prompt is currently shown.
    if dry_run:
        click.echo(
            f"[dry-run] would save account '{name}' (email={email or 'auto-detect'})"
        )
        return
    import shutil
    from pathlib import Path

    from .._state.account_store import _store_path, save_account

    home = Path.home()
    store = _store_path(None, home)
    cred_dir = store / name
    cred_dir.mkdir(parents=True, exist_ok=True)

    # Copy credential files into the snapshot directory
    claude_dir = home / ".claude"
    copied = []
    for fname in (".credentials.json",):
        src = claude_dir / fname
        if src.exists():
            shutil.copy2(src, cred_dir / fname)
            copied.append(fname)

    # Save metadata
    meta: dict = {}
    if email:
        meta["email_address"] = email
    else:
        # Try to read from current credentials
        # stx-allow: fallback (reason: reading existing email from credentials is best-effort; account save must still succeed without it)
        try:
            from .._account.credentials import read_credentials_metadata

            m = read_credentials_metadata(home=home)
            if m.get("email_address"):
                meta["email_address"] = m["email_address"]
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            pass

    save_account(name, meta, home=home)

    # Rotation audit: `save` snapshots a live login into the store — record
    # it (best-effort, never fails the save). Only an opaque fingerprint of
    # the snapshotted access token is recorded, never the token itself.
    if ".credentials.json" in copied:
        # stx-allow: fallback (reason: audit is a durable side-record; a
        # failure to write it must never fail the account save.)
        try:
            from .._account._rotation_audit import (
                fingerprint_token,
                log_rotation_event,
            )
            from .._account.claude_usage import _read_tokens_at

            access, _refresh, _cid, _exp = _read_tokens_at(
                cred_dir / ".credentials.json"
            )
            log_rotation_event(
                store=store,
                event="save",
                from_account=meta.get("email_address") or name,
                to_account=name,
                reason="account save (manual snapshot of live login into store)",
                to_token_fp=fingerprint_token(access),
            )
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            pass

    click.echo(
        f"Saved account '{name}' to {cred_dir} (files: {copied or 'none found'})"
    )


# ---------------------------------------------------------------------------
# list — Stored-accounts table + usage bars + fleet line. Lives in its own
# module (per-file line cap) since the 2026-07-11 dedupe redesign; attached
# onto the group at import time like refresh / sync-live.
# ---------------------------------------------------------------------------
from ._account_list_cmd import register_list_command

register_list_command(account)


@account.command("delete")
@click.argument("name")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be deleted without removing anything.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def account_delete(name: str, dry_run: bool, yes: bool) -> None:
    """Remove a stored account.

    \b
    Example:
      $ sac account delete work
      $ sac account delete work --dry-run
      $ sac account delete work --yes
    """
    from .._state.account_store import delete_account

    if dry_run:
        click.echo(f"[dry-run] would delete account '{name}'")
        return
    if not yes:
        click.echo(f"Refusing to delete account '{name}' without --yes/-y.", err=True)
        raise SystemExit(2)
    if delete_account(name):
        click.echo(f"Deleted account '{name}'")
    else:
        click.echo(f"Account '{name}' not found", err=True)
        raise SystemExit(1)


@account.command("switch")
@click.argument("name")
def account_switch(name: str) -> None:
    """Switch active credentials to a stored account.

    \b
    Example:
      $ sac account switch work
    """
    from .._state.account_store import switch_account

    result = switch_account(name)
    if result["success"]:
        click.echo(result["message"])
    else:
        click.echo(result["message"], err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# mint-token — master-side ACCESS-ONLY credential minting. Lives in its own
# module (like refresh / sync-live) to keep this file under the per-file
# line cap; attached onto the group at import time.
# ---------------------------------------------------------------------------
from ._account_mint_token import register_mint_token_command

register_mint_token_command(account)


# ---------------------------------------------------------------------------
# keepalive — DELIVER that access-only artifact to peers and prove the far
# side accepts it. `mint-token` produces the shape; this is what keeps the
# access-only hosts from silently expiring. Own module (per-file line cap);
# attached at import time like mint-token / refresh.
# ---------------------------------------------------------------------------
from ._account_keepalive import register_keepalive_command

register_keepalive_command(account)


# ---------------------------------------------------------------------------
# login — semi-automated `claude /login` re-auth. Drives claude in a tmux
# pane, extracts + delivers the OAuth URL to the operator, awaits the
# browser/code step, then reuses `account save`. Lives in its own module
# (per-file line cap); attached at import time like refresh / mint-token.
# ---------------------------------------------------------------------------
from ._account_login import register_login_command

register_login_command(account)


# ---------------------------------------------------------------------------
# quota-watch — exposed both at top-level (legacy `quota_watch`, re-exported
# below) and under ``account``. Bodies extracted to ``_account_quota_watch``
# to keep this file under the per-file line cap.
# ---------------------------------------------------------------------------
from ._account_quota_watch import quota_watch, register_quota_watch_commands

register_quota_watch_commands(account)


# ---------------------------------------------------------------------------
# status — one-shot quota snapshot, optionally over ssh to a peer.
# ---------------------------------------------------------------------------


@account.command("status")
@click.option(
    "--host",
    "host",
    default=None,
    help=(
        "Peer name from ~/.scitex/agent-container/config.yaml. When set, "
        "ssh to that peer and run `sac accounts status --json` there."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON object instead of human prose.",
)
def account_status(host: str | None, as_json: bool) -> None:
    """One-shot quota snapshot (5h%, 7d%, account email + tier).

    \b
    Examples:
      $ sac accounts status
      $ sac accounts status --json
      $ sac accounts status --host spartan
    """
    import json as _json

    from ._account_status import (
        StatusError,
        collect_status,
        collect_status_remote,
        format_status_prose,
    )

    try:
        if host is not None:
            snapshot = collect_status_remote(host)
        else:
            snapshot = collect_status()
    except StatusError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1)

    if as_json:
        click.echo(_json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        click.echo(format_status_prose(snapshot))


# ---------------------------------------------------------------------------
# refresh — headless OAuth access-token rotation (no `claude /login` prompt).
# Lives in its own module to keep this file under the per-file line cap;
# attached onto the group at import time (same pattern as sync-live).
# ---------------------------------------------------------------------------
from ._account_refresh import register_refresh_command

register_refresh_command(account)


# ---------------------------------------------------------------------------
# refresh-quota-cache — the PRODUCER for the aggregate quota-cache.json that
# the quota-aware boot picker (_creds/_pick_healthy) + a2a metadata enricher
# read. Without a periodic run of this, that file never exists and the picker
# degrades to freshness-only. Lives in its own module (per-file line cap);
# attached at import time like refresh / sync-live. A host cron runs it.
# ---------------------------------------------------------------------------
from ._account_refresh_quota_cache import register_refresh_quota_cache_command

register_refresh_quota_cache_command(account)


# ---------------------------------------------------------------------------
# quota — agent self-awareness: read THIS agent's own account quota from
# the bound quota-cache.json (#16 PART 4). Reads $CLAUDE_AGENT_ACCOUNT
# (injected by SAC at launch; see config/_loaders.py) and looks up the
# matching entry in /var/sac/quota-cache.json (bound by the apptainer
# runtime). Mirrors the host-cron schema — h5/d7 utilization %, ttl_h.
#
# Never errors on missing data: prints "unavailable" + non-zero exit
# only when the operator passes --strict. Default exit code 0 + JSON
# null/object so a Claude turn can run `sac account quota --json` in a
# pipeline without aborting on a cold cache.
# ---------------------------------------------------------------------------


@account.command("quota")
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit JSON instead of human-readable text.",
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help=(
        "Exit non-zero when no quota entry resolves (unset "
        "$CLAUDE_AGENT_ACCOUNT, missing cache file, no matching entry). "
        "Default behaviour: exit 0 with 'unavailable' / JSON null."
    ),
)
def account_quota(json_out: bool, strict: bool) -> None:
    """Print THIS agent's own account + live quota numbers.

    Reads ``$CLAUDE_AGENT_ACCOUNT`` (injected by SAC at launch) and looks
    up the matching entry in ``/var/sac/quota-cache.json`` (bound
    read-only by the apptainer runtime; the host cron refreshes the
    backing file every 10 minutes).

    \b
    Examples:
      $ sac account quota
      account=alpha 5h=17 percent 7d=3 percent ttl=7.74h

      $ sac account quota --json
      {"account":"alpha","used_pct_5h":17.0,"used_pct_7d":3.0,"token_ttl_hours":7.74}
    """
    import json as _json
    import sys

    from .._account.quota_cache import build_a2a_metadata

    meta = build_a2a_metadata()
    if not meta:
        if json_out:
            click.echo("null")
        else:
            click.echo("unavailable")
        if strict:
            sys.exit(1)
        return

    if json_out:
        # Keep the JSON key order stable so downstream consumers (Claude
        # turns piping through `jq`, smoke-tests grepping for fields)
        # see a deterministic shape.
        ordered = {
            "account": meta["account"],
            "used_pct_5h": meta["used_pct_5h"],
            "used_pct_7d": meta["used_pct_7d"],
            "token_ttl_hours": meta["token_ttl_hours"],
        }
        click.echo(_json.dumps(ordered))
    else:
        # Compact, parseable-by-eye TTY line. Percentages rounded to
        # match the telegrammer signature shape (operator's wire example
        # used integer percents); TTL is shown with 2 decimal places so
        # a "0.5h left" warning is legible.
        click.echo(
            f"account={meta['account']} "
            f"5h={round(meta['used_pct_5h'])} percent "
            f"7d={round(meta['used_pct_7d'])} percent "
            f"ttl={meta['token_ttl_hours']:.2f}h"
        )


# ``quota_watch`` is re-exported (defined in ``_account_quota_watch``) so the
# lazy entry-point path ``account_group:quota_watch`` in ``_main.py`` keeps
# resolving after the body was extracted. Named in ``__all__`` so linters do
# not flag the re-export as an unused import.
__all__ = ["account", "quota_watch"]
