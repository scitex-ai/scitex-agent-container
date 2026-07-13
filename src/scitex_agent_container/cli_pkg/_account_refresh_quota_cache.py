"""``sac accounts refresh-quota-cache`` — populate the aggregate quota cache.

Split out of ``account_group.py`` to keep that orchestrator under the
per-file line cap; the command is attached onto the ``account`` group at
import time via :func:`register_refresh_quota_cache_command` (same pattern as
``_account_refresh.register_refresh_command``).

This is the missing PRODUCER for the aggregate ``quota-cache.json`` that the
quota-aware boot picker (:mod:`_creds._pick_healthy`) and the a2a metadata
enricher read via :func:`_account.quota_cache.read_quota_entry`. Without a
periodic run of this command that file never exists → the picker sees
"unknown" for every account and silently degrades to freshness-only. A host
cron (every 15-30 min) should run this so the picker stays current.

Fail-loud per account: one account's fetch failure is printed and the loop
continues; the run exits non-zero only when EVERY attempted account failed.
Token values are never printed — only percentages + TTL hours.
"""

from __future__ import annotations

import click


@click.command("refresh-quota-cache")
@click.option(
    "--cache-path",
    "cache_path",
    default=None,
    help=(
        "Override where the aggregate quota-cache.json is written. Defaults "
        "to $SAC_QUOTA_CACHE_PATH, then ~/.scitex/quota-cache.json (the host "
        "file the apptainer runtime binds into each agent)."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON object on stdout instead of human prose.",
)
def account_refresh_quota_cache(cache_path: str | None, as_json: bool) -> None:
    """Fetch each stored account's usage and (re)write quota-cache.json.

    Walks every stored account, fetches its live 5h/7d utilisation from the
    Anthropic usage API (reusing the per-account credential-swap fetch), reads
    the token TTL from the snapshot, and writes the aggregate quota-cache.json
    that the quota-aware boot picker reads. A host cron should run this every
    15-30 minutes so the picker stays current.

    \b
    Examples:
      $ sac accounts refresh-quota-cache
      $ sac accounts refresh-quota-cache --json
    """
    import json as _json

    from .._account.quota_cache_refresh import refresh_quota_cache

    result = refresh_quota_cache(cache_path=cache_path)

    if as_json:
        click.echo(_json.dumps(result, ensure_ascii=False, indent=2))
    else:
        results = result["results"]
        if not results:
            click.echo(
                "No accounts stored. Use: sac accounts save <name> "
                f"(wrote empty cache to {result['cache_path']})"
            )
        for row in results:
            if row["error"] is None:
                click.echo(
                    f"  {row['name']:20s}  5h={row['h5']:.0f}% "
                    f"7d={row['d7']:.0f}% ttl={row['ttl_h']:.2f}h"
                )
            else:
                click.echo(
                    f"  {row['name']:20s}  FAILED — {row['error']}", err=True
                )
        click.echo(
            f"wrote {result['ok']} account(s) to {result['cache_path']} "
            f"({result['failed']} failed)"
        )

    # Exit non-zero only when EVERY attempted account failed — a partial
    # success (some accounts written) is still useful for the picker.
    attempted = result["ok"] + result["failed"]
    if attempted > 0 and result["ok"] == 0:
        raise SystemExit(1)


def register_refresh_quota_cache_command(group: click.Group) -> None:
    """Attach the ``refresh-quota-cache`` command onto the ``account`` group."""
    group.add_command(account_refresh_quota_cache)


__all__ = [
    "account_refresh_quota_cache",
    "register_refresh_quota_cache_command",
]
