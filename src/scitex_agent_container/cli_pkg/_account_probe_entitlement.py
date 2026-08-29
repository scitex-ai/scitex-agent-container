"""``sac accounts probe-entitlement`` — the PRODUCER of the verdicts.

The boot picker only ever READS a cached entitlement verdict (see
:mod:`.._creds._entitlement` for why it must never probe live at
start-up). Something has to write those verdicts, and this is it.

Without a periodic run of this command the file never exists, every
account reads ``UNKNOWN``, and the entitlement gate is inert — exactly
the failure mode ``refresh-quota-cache`` documents for the quota cache.
It is meant to be attached to the host timer that already walks every
account.

WHY IT IS A SEPARATE VERB FROM ``refresh``. Refresh rotates a
single-use OAuth token; this makes a read-only request and rotates
nothing. Keeping them separate means a de-entitled account can be
re-checked as often as we like without touching credential material,
and a probe failure can never cost us a token.

INCIDENT 2026-08-25 is the reason the verb exists: a cancelled
subscription kept refreshing its token successfully and so passed every
freshness gate while returning 403 on every real turn.
"""

from __future__ import annotations

import click


def register_probe_entitlement_command(group) -> None:
    """Attach ``probe-entitlement`` onto the ``accounts`` group."""

    @group.command("probe-entitlement")
    @click.option(
        "--all",
        "all_accounts",
        is_flag=True,
        default=False,
        help="Probe every stored account (what the host timer runs).",
    )
    @click.argument("name", required=False)
    @click.option(
        "--json",
        "as_json",
        is_flag=True,
        default=False,
        help="Emit a JSON array of verdicts.",
    )
    def probe_entitlement_cmd(
        all_accounts: bool, name: str | None, as_json: bool
    ) -> None:
        """Ask each account whether it may still RUN, and cache the answer.

        Freshness and entitlement are different questions. A cancelled
        subscription refreshes its OAuth token perfectly well, so it
        looks healthy to every freshness gate while returning 403 for
        an actual turn. This command asks the second question and
        writes the verdict beside the credential, where the boot picker
        reads it.

        Read-only: it rotates nothing and cannot cost a token.

        Verdicts are three-valued. Only a measured 403 naming an
        OAuth/permission error becomes FORBIDDEN. A timeout, a 5xx, a
        429 or a missing credential is UNKNOWN, and UNKNOWN never takes
        an account out of service — a network blip is not a cancelled
        subscription.

        Re-running after the subscription is restored flips the verdict
        back on its own; nothing else has to be edited.

        \b
        Examples:
          $ sac accounts probe-entitlement --all
          $ sac accounts probe-entitlement wyusuuke-gmail-com
          $ sac accounts probe-entitlement --all --json
        """
        import json as _json
        from pathlib import Path

        from .._creds._entitlement import (
            FORBIDDEN,
            probe_entitlement,
            write_entitlement,
        )
        from .._state.account_store import _store_path, list_accounts

        store = _store_path(None, Path.home())

        if name:
            names = [name]
        elif all_accounts:
            names = [a["name"] for a in list_accounts()]
        else:
            raise click.UsageError("give an account NAME or pass --all")

        results = []
        any_forbidden = False
        for acct in names:
            acct_dir = store / acct
            verdict = probe_entitlement(acct, acct_dir)
            written = write_entitlement(acct_dir, verdict)
            if verdict.state == FORBIDDEN:
                any_forbidden = True
            results.append(
                {
                    "account": acct,
                    "state": verdict.state,
                    "http_status": verdict.http_status,
                    "detail": verdict.detail,
                    "recorded": written,
                }
            )

        if as_json:
            click.echo(_json.dumps(results, indent=2))
        else:
            for r in results:
                click.echo(
                    f"  {r['account']:<28} {r['state']:<10} {r['detail']}"
                )
            if any_forbidden:
                # Say what it MEANS, not just what it is. "FORBIDDEN"
                # alone reads as our bug; the operator needs to know it
                # is a subscription, that the fleet already routed
                # around it, and that restoring it needs no cleanup.
                click.echo("")
                click.echo(
                    "  A FORBIDDEN account is out of the rotation pool from "
                    "now on. Its credentials are untouched; restore the "
                    "subscription and the next run of this command puts it "
                    "back automatically."
                )

        # Exit 0 even with a FORBIDDEN result: recording a cancelled
        # subscription is this command SUCCEEDING. A non-zero exit would
        # mark the timer unit `failed` on every pass for as long as the
        # operator chose to keep a subscription cancelled — the exact
        # "unit reports failure while doing its job correctly" trap that
        # `anthropic/`-as-an-account caused on 2026-07-29, which trains
        # readers to ignore the unit.
