"""``sac accounts list`` — the CLI command body.

Split out of ``account_group.py`` to keep that orchestrator under the
per-file line cap; the command is attached onto the ``account`` group at
import time via :func:`register_list_command` (same pattern as
``_account_refresh.register_refresh_command``).

Human-output layout (operator directive 2026-07-11 — "the bars own the
percentages; the table holds only what the bars cannot express"):

1. The single-account "Claude Code account" header block (active
   credentials; untouched by the 2026-07-11 redesign).
2. The Stored-accounts table — exactly ``Account | Status | Last
   Update`` (:func:`._account_list_render.render_stored_table`).
3. The usage-bars block — per-account 5h/7d bars, each percentage
   carrying its compact reset hint (``29% (→09:19)`` /
   ``66% (→Sun 21h)``), plus the rolling-window legend when a row has
   no cached reset timestamps
   (:mod:`._account_usage_bars` / :func:`._account_list_render.rolling_legend_line`).
4. The one-line fleet effective-utilization figure.

The JSON path (``sac accounts list --json``) is schema-stable and keeps
``email_address`` / ``plan_label`` / the raw usage payload for machine
consumers.
"""

from __future__ import annotations

import click


@click.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON array on stdout instead of human prose.",
)
@click.option(
    "--refresh",
    "--live",
    "refresh",
    is_flag=True,
    default=False,
    help=(
        "Force a fresh upstream usage fetch by discarding the per-account "
        "usage.json cache before rendering. Without this flag the 5-min "
        "cache is consulted to avoid hammering the API; the Last Update "
        "column always shows the snapshot age so a stale number is obvious."
    ),
)
def account_list(as_json: bool, refresh: bool) -> None:
    """List stored accounts and show the currently active one.

    The human view splits its two surfaces without duplication
    (operator directive 2026-07-11): the Stored-accounts table is
    exactly Account | Status | Last Update — the status cell carries
    the live token TTL (``VALID +2h26m``) — while the monospace
    usage-bars block below it owns the 5h/7d percentages, each with
    its compact per-window reset hint (``29% (→09:19)`` /
    ``66% (→Sun 21h)``), followed by a single fleet
    effective-utilization line.

    \b
    Fleet effective utilization
      A reset-horizon-weighted fleet figure. Per account, over a 7-day
      planning window W: frac_before_reset = clamp(reset_horizon, 0, W)/W
      and effective% = frac_before_reset * used_pct_7d. So an account at
      100% that resets in 1 day (eff ~14%) contributes far more usable
      weekly capacity than one at 100% resetting in 6 days (eff ~86%).
      The reset horizon is the true 7d-window reset (reset_at_7d); when
      absent it defaults to the full window (eff = used_pct_7d). The
      fleet figure is the mean over accounts with cached usage.

    \b
    Example:
      $ sac account list
      $ sac account list --json
      $ sac account list --refresh    # force upstream usage% refetch
    """
    import json as _json

    from .._account.credentials import read_credentials_metadata
    from .._state.account_store import list_accounts
    from ._account_list_render import (
        build_stored_json,
        build_stored_rows,
        needs_rolling_legend,
        render_stored_table,
        rolling_legend_line,
    )
    from ._account_usage_bars import fleet_effective_line, render_usage_bars_block
    from ._helpers import console
    from .status_cmds import _format_claude_account_block

    accounts = list_accounts()

    if as_json:
        # stx-allow: fallback (reason: malformed credentials JSON tolerated)
        try:
            active = read_credentials_metadata()
        except (OSError, _json.JSONDecodeError):
            active = {}
        click.echo(
            _json.dumps(
                {
                    "active": active,
                    "stored": build_stored_json(accounts, refresh=refresh),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    # Active credentials block
    # stx-allow: fallback (reason: malformed credentials JSON tolerated; section omitted on error)
    try:
        active_meta = read_credentials_metadata()
    except (OSError, _json.JSONDecodeError):
        active_meta = {}
    lines = _format_claude_account_block(active_meta)
    for line in lines:
        console.print(line)
    if lines:
        console.print("")

    if not accounts:
        click.echo(
            "No accounts stored. Use: scitex-agent-container account save <name>"
        )
        return
    rows = build_stored_rows(accounts, refresh=refresh)
    console.print(render_stored_table(rows))
    # Operator directive 2026-07-11: the bars own the percentages AND
    # their reset hints; the table above holds only what the bars
    # cannot express. Emitted via click.echo (NOT console.print) so
    # the `[..]` bar brackets render literally instead of being parsed
    # as rich markup.
    bars_block = render_usage_bars_block(rows)
    if bars_block:
        click.echo("")
        click.echo(bars_block)
    # When the upstream usage API didn't return per-row reset
    # timestamps (older caches / API outage), the per-line `(→...)`
    # hint can't render. Print a one-line legend below the bars so the
    # operator still sees the rolling-window contract instead of
    # guessing.
    if needs_rolling_legend(rows):
        click.echo(rolling_legend_line())
    click.echo(fleet_effective_line(rows))


def register_list_command(group: click.Group) -> None:
    """Attach the ``list`` command onto the ``account`` group."""
    group.add_command(account_list)


__all__ = [
    "account_list",
    "register_list_command",
]
