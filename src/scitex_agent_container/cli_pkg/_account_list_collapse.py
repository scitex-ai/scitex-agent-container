"""One row per ACCOUNT for ``sac accounts list``, not one per host×account.

Operator directive 2026-08-17: 「アカウントは明らかに 4 つしかないですよね？
なのにホスト毎に出てしまって醜いです。」 — there are plainly only four
accounts, so printing them once per host is ugly.

WHAT WAS WRONG WITH THE OLD TABLE. It rendered the cross product: four
accounts times every host that answered, so a healthy five-host fleet
produced twenty rows carrying four accounts' worth of information. Worse,
the width that cross product needed came out of the Host column, which rich
abbreviated to ``scitex-comp…`` — making compute-01/02/03/04 render
identically, so the table did not merely repeat itself, it looked like it
was repeating rows it was not. Measured 2026-08-17: sixteen rows, sixteen
DISTINCT (host, account) pairs, zero duplicates in the data.

WHAT COLLAPSES AND WHAT MUST NOT. The distinction is which side of the wire
each fact lives on:

* Identity and usage are properties of the ANTHROPIC ACCOUNT. Every host
  reading them is reading the same upstream fact through its own cache, so
  showing them once is not a summary — it is the fact stated once instead
  of five times, and the freshest cache is the best available reading of it.
* Credential freshness is a property of the FILE ON ONE MACHINE. Credentials
  are per-host files and are not on the sync rail, so the same account is
  routinely VALID on one machine and EXPIRED on another (measured
  2026-08-14, when a restart refused with "no healthy stored account" while
  the identical accounts were hours-fresh elsewhere).

So this module collapses the first kind and REFUSES to collapse the second:
when hosts agree, the status cell states the agreed value; when they differ,
it names the hosts that differ rather than picking a representative. A
collapse that hid an expired credential behind three valid ones would turn
the ugly-but-honest table into a compact lie, and the exploded view exists
(``--by-host``) for when the per-host detail is the question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rich.table import Table

from ._account_list_format import format_as_of_short, format_snapshot_age
from ._account_list_render import AccountRow, _fmt_status

__all__ = ["AccountGroup", "collapse_by_account", "render_accounts_table"]


@dataclass
class AccountGroup:
    """Every host's reading of ONE account, kept separable.

    The per-host rows are retained rather than reduced on construction so
    the renderer can decide, per column, whether the hosts agreed — a
    pre-reduced group could not tell "all four say VALID" apart from "one
    said VALID and the rest were dropped".
    """

    provider: str
    name: str
    rows: list[AccountRow] = field(default_factory=list)

    @property
    def hosts(self) -> list[str]:
        return [r.host for r in self.rows if r.host]


def collapse_by_account(rows: list[AccountRow]) -> list[AccountGroup]:
    """Group rows by (provider, account), preserving first-seen order."""
    groups: dict[tuple[str, str], AccountGroup] = {}
    for row in rows:
        key = (row.provider, row.name)
        if key not in groups:
            groups[key] = AccountGroup(provider=row.provider, name=row.name)
        groups[key].rows.append(row)
    return list(groups.values())


def _fmt_hosts_cell(group: AccountGroup, all_hosts: list[str]) -> str:
    """Say how many hosts hold this account, and NAME the ones that do not.

    A bare count would answer "how many" while hiding "which", and the host
    MISSING a credential is the actionable one — that is the host whose next
    agent start refuses with "no healthy stored account".
    """
    present = [h for h in all_hosts if h in set(group.hosts)]
    if not all_hosts:
        return "-"
    missing = [h for h in all_hosts if h not in set(group.hosts)]
    if not missing:
        return f"all {len(present)}"
    return f"{len(present)}/{len(all_hosts)} (not on {', '.join(missing)})"


def _fmt_status_cell(group: AccountGroup) -> str:
    """The agreed credential status, or the disagreement spelled out.

    Never reduces divergence to a representative value: an EXPIRED
    credential hidden behind three VALID ones is the one thing this column
    exists to surface.
    """
    states = {r.freshness_state for r in group.rows}
    if len(states) == 1:
        return _fmt_status(group.rows[0].freshness_state, group.rows[0].freshness_hours)
    majority = max(states, key=lambda s: sum(r.freshness_state == s for r in group.rows))
    odd = [r for r in group.rows if r.freshness_state != majority]
    detail = ", ".join(f"{r.freshness_state} on {r.host or '?'}" for r in odd)
    return f"{majority} x{len(group.rows) - len(odd)}; {detail}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # stx-allow: fallback (reason: an unparseable cache timestamp must not
    # abort the whole table; it simply cannot win the freshest-snapshot pick)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _freshest_snapshot(group: AccountGroup) -> str | None:
    """The newest usage snapshot any host holds for this account.

    Usage lives on Anthropic's side; a host only caches it. So the newest
    cache is the best available reading of one shared fact, not a sample
    chosen from several competing ones.
    """
    dated = [(dt, r.snapshot_as_of) for r in group.rows if (dt := _parse_iso(r.snapshot_as_of))]
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[0])[1]


def _fmt_identity_cell(group: AccountGroup) -> str:
    """Identity is an account-level fact, so it is stated once.

    A mismatch or duplicate reported by ANY host wins over the verified
    readings, because a credential that authenticates as the wrong account
    on one machine is a wrong credential, not a minority opinion.
    """
    for row in group.rows:
        if row.duplicate_of:
            return f"DUPLICATE of {row.duplicate_of}"
    for row in group.rows:
        if row.identity_state == "mismatch":
            return f"MISMATCH -> {row.verified_email or 'another account'}"
    verified = [r for r in group.rows if r.identity_state == "verified"]
    if not verified:
        return "unverified"
    email = verified[0].verified_email
    if not email:
        return "ok"
    # Account IDs are email-derived slugs, so a verified identity that matches
    # its slug restates the Account column one punctuation change apart
    # (`scitex-01-scitex-ai` / `scitex-01@scitex.ai`). Say `ok` instead and
    # spend the width on the case that carries a fact: an email that does NOT
    # match the label it is filed under.
    from .._account.creds_sync import slugify_email

    return "ok" if slugify_email(email) == group.name else email


def render_accounts_table(
    rows: list[AccountRow],
    *,
    now: datetime | None = None,
) -> Table:
    """Build the collapsed table: ``Provider | Account | Identity | Status |
    Usage as of | Hosts``.

    ``now`` is an injection seam so snapshot-age cells are deterministic in
    tests without patching the clock.
    """
    groups = collapse_by_account(rows)
    all_hosts: list[str] = []
    for row in rows:
        if row.host and row.host not in all_hosts:
            all_hosts.append(row.host)

    table = Table(title="Stored accounts", title_justify="left", show_lines=False)
    table.add_column("Provider")
    table.add_column("Account", style="bold")
    table.add_column("Identity")
    table.add_column("Status")
    table.add_column("Usage as of")
    if all_hosts:
        table.add_column("Hosts", style="cyan")
    for group in groups:
        as_of = _freshest_snapshot(group)
        cells = [
            group.provider,
            group.name,
            _fmt_identity_cell(group),
            _fmt_status_cell(group),
            (
                f"{format_as_of_short(as_of)} ({format_snapshot_age(as_of, now=now)})"
                if as_of
                else "-"
            ),
        ]
        if all_hosts:
            cells.append(_fmt_hosts_cell(group, all_hosts))
        table.add_row(*cells)
    return table
