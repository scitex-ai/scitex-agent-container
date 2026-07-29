"""``sac accounts list`` — the CLI command body.

Split out of ``account_group.py`` to keep that orchestrator under the
per-file line cap; the command is attached onto the ``account`` group at
import time via :func:`register_list_command` (same pattern as
``_account_refresh.register_refresh_command``).

Human-output layout (operator directive 2026-07-11 — "the bars own the
percentages; the table holds only what the bars cannot express"):

1. Active Claude Code and OpenAI Codex account blocks, when present.
2. The combined accounts table — exactly ``Provider | Account | Status |
   Last Update`` (:func:`._account_list_render.render_stored_table`).
3. The usage-bars block — one 3-line block per account (operator
   mockup 2026-07-17): the account name, then one line per window with
   the relative reset hint BEFORE the bar (``5h (in 4h05m) [..] (29%)``),
   a blank line between accounts; plus the rolling-window legend when a
   row has no cached reset timestamps
   (:mod:`._account_usage_bars` / :func:`._account_list_render.rolling_legend_line`).
4. The one-line fleet 7-day capacity-used figure.

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
    (operator directive 2026-07-11): the accounts table is exactly
    Provider | Account | Status | Last Update — the status cell carries
    the live token TTL (``VALID +2h26m``) for Claude accounts — while the monospace
    usage-bars block below it owns the 5h/7d percentages, one 3-line
    block per account with the relative reset hint before each bar
    (``5h (in 4h05m) [..] (29%)``; operator mockup 2026-07-17),
    followed by a single fleet 7-day capacity-used line.

    \b
    Fleet 7d capacity used
      How much of the fleet's weekly capacity was actually consumed over
      the trailing 7 days — a capacity-planning signal: ~100% ⇒ the
      fleet is saturated (add an account); low ⇒ over-provisioned (drop
      one). It is the arithmetic mean of the accounts' 7d-window
      utilisation (the same used_pct_7d the 7d bars show), over the
      accounts with cached usage. The API exposes utilisation
      percentages (not absolute quotas), so the mean of the percentages
      is the aggregate; when quotas match it equals total-used /
      total-available.

    \b
    Example:
      $ sac account list
      $ sac account list --json
      $ sac account list --refresh    # force upstream usage% refetch
    """
    import json as _json

    from .._account.codex_account import (
        CodexAccountSyncError,
        read_codex_accounts_metadata,
    )
    from .._account.credentials import read_credentials_metadata
    from .._state.account_store import list_accounts
    from ._account_list_render import (
        build_openai_rows,
        build_provider_accounts_json,
        build_stored_json,
        build_stored_rows,
        render_stored_table,
    )
    from ._account_openai import format_openai_account_block
    from ._account_usage_bars import render_usage_bars_block
    from ._helpers import console
    from .status_cmds import _format_claude_account_block

    accounts = list_accounts()
    # An UNREADABLE OpenAI store must not delete the Claude view. This command is
    # the operator's primary credential instrument and is reached most often
    # DURING an incident, which is exactly when a store is half-written, revoked,
    # or logged out. `read_codex_accounts_metadata` raises for every such state
    # once the store root exists (and `sac accounts sync-openai` creates it
    # permanently), so an unguarded call here took the whole command down —
    # including `--json`, which sits below this line. The Claude reads on this
    # same path have always been exception-tolerant; the provider axis has to
    # meet that contract, not lower it.
    #
    # Degrade to a THIRD state rather than to absence. `[]` with no error means
    # "no OpenAI accounts", which is a true and useful answer; `[]` because the
    # read failed is a different fact, and rendering them identically would tell
    # the operator their store is empty when it is broken.
    # stx-allow: fallback (reason: unreadable OpenAI store degrades to an error row; the Claude view must survive it)
    openai_error: str | None = None
    try:
        openai_accounts = read_codex_accounts_metadata()
    except CodexAccountSyncError as exc:
        openai_accounts = []
        openai_error = str(exc)
    openai_meta = openai_accounts[0] if openai_accounts else {}

    if as_json:
        # stx-allow: fallback (reason: malformed credentials JSON tolerated)
        try:
            active = read_credentials_metadata()
        except (OSError, _json.JSONDecodeError):
            active = {}
        stored_json = build_stored_json(accounts, refresh=refresh)
        click.echo(
            _json.dumps(
                {
                    "active": active,
                    "openai": openai_meta,
                    "openai_accounts": openai_accounts,
                    "openai_error": openai_error,
                    "stored": stored_json,
                    "accounts": build_provider_accounts_json(
                        stored_json, openai_accounts
                    ),
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

    if openai_error is not None:
        console.print(f"[yellow]OpenAI accounts UNREADABLE:[/yellow] {openai_error}")
        console.print(
            "[dim]The Claude accounts below are unaffected. "
            "Repair with: sac accounts sync-openai[/dim]"
        )
        console.print("")

    for openai_account in openai_accounts:
        openai_lines = format_openai_account_block(openai_account)
        for line in openai_lines:
            console.print(line)
        if openai_lines:
            console.print("")

    rows = build_stored_rows(accounts, refresh=refresh)
    all_rows = rows + build_openai_rows(openai_accounts)
    if not all_rows:
        click.echo(
            "No accounts stored or active. Use: "
            "scitex-agent-container account save <name>"
        )
        return
    console.print(render_stored_table(all_rows))
    # Operator directive 2026-07-11: the bars own the percentages AND
    # their reset hints; the table above holds only what the bars
    # cannot express. Emitted via click.echo (NOT console.print) so
    # the `[..]` bar brackets render literally instead of being parsed
    # as rich markup.
    bars_block = render_usage_bars_block(rows)
    if bars_block:
        click.echo("")
        click.echo(bars_block)
    # Legend and the `Fleet 7d capacity used:` line both DROPPED
    # (operator 2026-07-30). The legend explained a rolling-window
    # contract the per-line `(in ...)` hints already carry, and the fleet
    # line stated in prose the number the new `- Average (n=N)` block now
    # renders as a bar — the same arithmetic mean, in the visual language
    # of the rest of the section instead of a sentence under it.
    #
    # `needs_rolling_legend` / `rolling_legend_line` are intentionally left
    # in _account_list_render for now: they are exported and separately
    # tested, and deleting them is a wider change than the display request.


def register_list_command(group: click.Group) -> None:
    """Attach the ``list`` command onto the ``account`` group."""
    group.add_command(account_list)


__all__ = [
    "account_list",
    "register_list_command",
]
