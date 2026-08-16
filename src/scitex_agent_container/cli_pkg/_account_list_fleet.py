"""Fleet-wide ``sac accounts list`` — the same fan-out, a different payload.

Operator, 2026-08-14: *"agents list だけでなく、accounts list もホスト間で同期
されているもので ACL が許すものを表してください."* So this rides the fan-out
``sac agents list`` already uses — :func:`.._helpers._agent_list_fleet.collect_fleet`
for the concurrency and the per-host reports,
:mod:`.._helpers._agent_list_fleet_render` for the mandatory header — rather
than growing a second mechanism beside it. Only two things here are
accounts-specific: WHAT we ask each host for, and how its answer becomes a row.

WHY ACCOUNTS NEED THIS MORE THAN AGENTS DO
------------------------------------------
An agent lives on one machine. A CREDENTIAL is a per-host FILE that is NOT on
the sync rail, so the SAME account is routinely valid on one host and expired
on another — and nothing showed you both at once. Measured 2026-08-14: a
restart on one host refused with *"no healthy stored account"* because all
three accounts had expired THERE, while the identical three were +4.9h / +4.9h
/ +2.9h fresh on another host. A fleet view would have shown that at a glance
instead of costing an outage. That is why every row names its host and carries
its own time-to-expiry.

TWO SAFETY PROPERTIES THIS FILE EXISTS TO HOLD
----------------------------------------------
1. **It never rotates a credential.** ``sac accounts list`` normally fetches
   usage, and that fetch refreshes an expired OAuth token, rewriting
   ``.credentials.json`` in place. The refresh token is single-use, so the
   server invalidates the old one and every agent still holding the old access
   token starts getting 401s — on that host AND on every host binding the same
   snapshot (INCIDENT 2026-08-09; see :mod:`._account_refresh_gate`). Doing
   that once is bad; a fleet fan-out would do it on every machine at once. So
   the fan-out passes ``--passive`` to each peer and uses ``passive=True``
   locally: freshness comes from :func:`.._account.creds_sync.account_freshness`
   (a pure read of ``expiresAt``) and usage from the on-disk cache. The header
   says the view is passive, so nobody mistakes a cached percentage for a live
   one.

2. **It never prints token material.** Rows carry the account slug, the
   provider, the freshness state and hours, the verified identity, and the
   host. Not the access token, not the refresh token, not the file's contents.
   The upstream builders already whitelist their fields and assert no secret
   escapes; this module adds no new field that could carry one, and the
   peer-payload reader below picks keys by NAME rather than copying the
   envelope through.
"""

from __future__ import annotations

import json as json_mod

import click

__all__ = ["fleet_account_options", "run_fleet_account_list", "rows_from_stored"]

# What we ask each peer for. ``--passive`` is the safety flag (property 1
# above); ``--no-fanout`` is the recursion guard, since the peer runs its own
# sac and would otherwise fan out again.
_REMOTE_ARGV = ("sac", "accounts", "list", "--json", "--passive", "--no-fanout")


def fleet_account_options(func):
    """Attach ``--host`` / ``--host-timeout`` / ``--no-fanout`` to the command.

    Deliberately the same three spellings ``sac agents list`` grew, with the
    same semantics, so an operator learns the fleet vocabulary once.
    """
    from ._helpers._agent_list_fleet_model import DEFAULT_HOST_TIMEOUT_S

    func = click.option(
        "--host-timeout",
        "host_timeout",
        type=float,
        default=DEFAULT_HOST_TIMEOUT_S,
        show_default=True,
        help=(
            "Fleet view: seconds to wait for EACH host. A host that exceeds it "
            "is reported as timed-out in the header and never blocks the rest "
            "of the listing."
        ),
    )(func)
    func = click.option(
        "--no-fanout",
        "no_fanout",
        is_flag=True,
        default=False,
        hidden=True,
        help=(
            "Fleet view: do not query peers; list only this host. Primarily the "
            "recursion guard the fan-out passes to each peer. The header always "
            "states when it is in force."
        ),
    )(func)
    func = click.option(
        "--by-host",
        "by_host",
        is_flag=True,
        default=False,
        help=(
            "Fleet view: one row per host AND account instead of one row per "
            "account. The collapsed default states each account once and names "
            "any host that disagrees; use this when the per-host detail — which "
            "machine holds which credential file — is itself the question."
        ),
    )(func)
    func = click.option(
        "--host",
        "hosts",
        multiple=True,
        metavar="HOSTNAME",
        help=(
            "Fleet view: only this host. Repeatable; exact match on the "
            "resolved hostname. 'localhost' / 'local' are accepted and RESOLVED "
            "at parse time, with the header echoing the resolution. An unknown "
            "name fails loudly, naming every host this machine can reach."
        ),
    )(func)
    return func


def rows_from_stored(stored: list[dict], host: str) -> list:
    """Turn one host's ``--json`` ``stored`` entries into renderable rows.

    Keys are read BY NAME rather than by copying the entry through, so a field
    this module has never heard of — including anything a hand-edited
    ``account.json`` might contain — cannot ride along into the table.

    ``used_pct_*`` is carried ONLY when the peer's own ``usage_state`` is
    ``known``. That mirrors the gate the local builder already applies: a
    percentage read with a credential that turns out to belong to a different
    account is not this account's usage, however freshly it was fetched, and it
    must not be rendered under the wrong name just because it survived an ssh
    hop.
    """
    from ._account_list_render import AccountRow

    rows = []
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        state = entry.get("usage_state") or "unknown"
        known = state == "known"
        identity = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
        rows.append(
            AccountRow(
                host=host,
                name=name,
                provider=str(entry.get("provider") or "claude-code"),
                freshness_state=str(entry.get("freshness") or "ABSENT"),
                freshness_hours=_as_float(entry.get("freshness_hours")),
                used_pct_5h=_as_float(usage.get("used_pct_5h")) if known else None,
                used_pct_7d=_as_float(usage.get("used_pct_7d")) if known else None,
                snapshot_as_of=_as_str(usage.get("as_of")),
                reset_at_5h=_as_str(usage.get("reset_at_5h")),
                reset_at_7d=_as_str(usage.get("reset_at_7d")),
                usage_state=str(state),
                usage_age_seconds=_as_int(entry.get("usage_age_seconds")),
                usage_reason=_as_str(entry.get("usage_unknown_reason")),
                identity_state=str(identity.get("state") or "unverified"),
                verified_email=_as_str(identity.get("verified_email")),
                duplicate_of=_as_str(identity.get("duplicate_of")),
            )
        )
    return rows


def _as_float(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _as_int(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _as_str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _peer_probe(target, timeout_s: float):
    """Ask ONE peer for its accounts over ssh. Returns ``(report, entries)``.

    Delegates every transport concern — the ``via:`` ProxyJump chain, the
    timeout mapping, the stale-peer retry, the RESPONDED / TIMED_OUT /
    UNREACHABLE / SAC_MISSING / MALFORMED verdicts — to the shared ssh probe.
    Only the argv and the envelope key differ from the agents leg.
    """
    from ._helpers._agent_list_fleet_probe import ssh_json_probe

    return ssh_json_probe(
        target,
        timeout_s,
        argv=list(_REMOTE_ARGV),
        envelope_key="stored",
        # ``--passive`` is load-bearing for SAFETY, so a peer that rejects it
        # must be REPORTED (sac_too_old), never re-asked without it: the
        # fallback would be the very credential rotation the flag prevents.
        required_flags=("--passive",),
    )


def _local_accounts(host: str):
    """This host's accounts, read PASSIVELY. Never refreshes a credential."""
    from .._state.account_store import list_accounts
    from ._account_list_build import build_stored_json

    return build_stored_json(list_accounts(), passive=True, host=host)


def run_fleet_account_list(
    *,
    use_json: bool,
    hosts: tuple[str, ...] = (),
    no_fanout: bool = False,
    host_timeout: float | None = None,
    local_extras: dict | None = None,
    openai_accounts: list[dict] | None = None,
    by_host: bool = False,
) -> None:
    """Collect every reachable host's accounts, print the header, then the rows.

    The header comes FIRST and unconditionally, in both surfaces: it is what
    makes an empty listing legible. With every host answered, no accounts means
    the fleet has none; with a host missing, it means the fleet is UNOBSERVED,
    and those two must never render the same way.

    An unreachable host never changes the exit code — a credential inventory
    that exits non-zero would break every caller that parses it, and the header
    already carries the truth.
    """
    from ._account_list_render import render_stored_table
    from ._helpers._agent_list_fleet import DEFAULT_HOST_TIMEOUT_S, collect_fleet
    from ._helpers._agent_list_fleet_model import UnknownHostFilter
    from ._helpers._agent_list_fleet_render import hosts_payload, print_fleet_header
    from ._helpers._console import console
    from ._helpers._agent_list_host import _resolve_display_host

    local_host = _resolve_display_host()
    try:
        listing = collect_fleet(
            hosts=hosts,
            no_fanout=no_fanout,
            host_timeout_s=(
                DEFAULT_HOST_TIMEOUT_S if host_timeout is None else host_timeout
            ),
            local_lister=lambda: _local_accounts(local_host),
            peer_probe=_peer_probe,
        )
    except UnknownHostFilter as exc:
        raise click.UsageError(str(exc)) from exc

    if use_json:
        from ._account_list_build import build_provider_accounts_json

        payload = dict(local_extras or {})
        # Same six keys the schema has always had, in the same order; `stored`
        # and the cross-provider `accounts` now span the fleet, and `hosts` is
        # the sibling that says whose answers are in them. A consumer that
        # reads `stored` without reading `hosts.responded` vs `hosts.total` is
        # treating a partial fleet as the whole one.
        payload["stored"] = listing.agents
        payload["accounts"] = build_provider_accounts_json(
            listing.agents, openai_accounts or []
        )
        payload["hosts"] = hosts_payload(listing)
        click.echo(json_mod.dumps(payload, ensure_ascii=False, indent=2))
        return

    print_fleet_header(console, listing)
    console.print(
        "[dim]passive read: freshness from each host's expiresAt, usage from "
        "its cache. Nothing here refreshes a token (a refresh rotates a "
        "single-use credential every other host is still using).[/dim]"
    )
    rows = []
    for entry in listing.agents:
        rows.extend(rows_from_stored([entry], str(entry.get("host") or "—")))
    if not rows:
        # WHICH empty is this? With every host answered, the fleet genuinely has
        # no accounts and the operator wants the next step. With a host missing,
        # the same blank table is an UNOBSERVED fleet, and telling him to go
        # create an account would be advice derived from a reading nobody took.
        if listing.responded == listing.total:
            click.echo(
                "No accounts stored or active. Use: "
                "scitex-agent-container account save <name>"
            )
        else:
            missing = ", ".join(r.host for r in listing.unanswered)
            console.print(
                f"[yellow]No accounts on the host(s) that answered — but "
                f"{missing} did not answer, so this is NOT evidence that the "
                f"fleet has none.[/yellow]"
            )
        return
    if by_host:
        console.print(render_stored_table(rows))
    else:
        from ._account_list_collapse import render_accounts_table

        console.print(render_accounts_table(rows))
