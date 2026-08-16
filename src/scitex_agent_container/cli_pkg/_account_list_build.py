"""Data acquisition + orchestration behind ``sac accounts list``.

Split out of :mod:`._account_list_render`, which had grown to hold three
responsibilities its own docstring already enumerated. The dividing line is
side effects: everything here touches the network, the credential store or
the clock, while what remains in ``_account_list_render`` is the row model
and the ``rich`` table built from it. That separation is what lets the
renderer be tested by hand-rolling an ``AccountRow`` with no monkeypatching
at all.

Every public name is re-exported from ``_account_list_render`` so existing
importers keep working unchanged.

Identity, and why it is fetched here
------------------------------------
INCIDENT 2026-08-12: ``sac accounts list`` showed ``ywatanabe-scitex-ai`` at
2 % weekly while the Anthropic console showed 92 % for that account. Neither
number was wrong — they described DIFFERENT accounts. The directory
``accounts/anthropic/ywatanabe-scitex-ai/`` held a credential belonging to
``ywata1989@gmail.com``, so one Anthropic account was rendered as two rows
and the fleet looked to have headroom it did not have.

A stored credential contains no identity claim of any kind, so nothing in
the store could have caught this. :func:`verify_stored_identities` asks the
authoritative source — the OAuth profile endpoint — and its answer gates the
percentages: a figure fetched with a credential that turns out to belong to
somebody else is not this row's usage, no matter how fresh it is.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ._account_list_render import AccountRow

# ``AccountRow`` is imported INSIDE the functions that construct it, not at
# module scope. ``_account_list_render`` re-exports this module's names for
# back-compat, so a module-scope import here would make the pair mutually
# importable — and a cycle resolves or explodes depending on which half the
# caller happens to import first, which is the worst kind of bug to own.
# ``from __future__ import annotations`` keeps the signatures readable
# without needing the runtime symbol.

# ---------------------------------------------------------------------------
# Per-account usage fetch
# ---------------------------------------------------------------------------


def _per_account_usage_cache_path(name: str):
    """Return the absolute path of the per-account ``usage.json`` cache."""
    from .._state.account_store import _store_path

    return _store_path(None, Path.home()) / name / "usage.json"


def usage_for_account(
    acct_meta: dict, *, refresh: bool = False, passive: bool = False
) -> dict | None:
    """Live PER-ACCOUNT usage fetch (5-min cache); ``--refresh`` busts it.

    ``passive=True`` READS AND NOTHING ELSE — the cache, never the network.

    That mode exists because THIS FUNCTION CAN ROTATE A CREDENTIAL. The
    ``fetch_usage_for_credentials`` call below refreshes the OAuth token when it
    is expired (and again on a 401), and that refresh rewrites the account's
    ``.credentials.json`` in place. The refresh token is SINGLE USE: the server
    invalidates the previous one, so every agent still holding the old access
    token — on this host and on every other host that binds the same snapshot —
    starts getting 401s. That is INCIDENT 2026-08-09, written up in
    :mod:`._account_refresh_gate`, whose ``needs_refresh`` gate guards
    ``sac accounts refresh`` and never guarded this path.

    A LISTING MUST NOT ROTATE ANYTHING, and a listing that fans out across the
    fleet must not do it N times at once, which is why the fleet view passes
    ``passive=True`` for every host including this one. The local single-host
    view keeps its historical behaviour so nothing an operator relies on
    changes silently.

    The snapshot lives at
    ``~/.scitex/agent-container/accounts/<name>/.credentials.json``
    (cascade-resolved via ``_store_path``); the fetch result is cached
    next to that file as ``usage.json`` so the same
    ``read_account_usage_cache`` reader sees the live value across
    invocations. Any failure (missing snapshot, expired token, network
    error) returns ``None`` → caller renders ``"-"`` for that row only;
    the rest of the list keeps rendering.

    When ``refresh`` is true the on-disk ``usage.json`` is removed
    before the fetch so the API is hit even when the cache is fresh —
    wiring for ``sac accounts list --refresh``.

    NOTE the fallback to ``read_account_usage_cache`` is UNBOUNDED in age
    by design — a figure from yesterday is still worth showing when the
    API is unreachable. What must never happen is showing it AS IF it were
    current, which is why every consumer runs the result through
    :func:`._account_usage_state.classify_usage` rather than reading
    ``used_pct_*`` directly.
    """
    from .._account.claude_usage import fetch_usage_for_credentials
    from .._state.account_store import _store_path, read_account_usage_cache

    name = acct_meta.get("name")
    if not name:
        return None
    if passive:
        # The ONLY statement on this branch, deliberately: everything below can
        # reach the network and can rewrite the credential. Returning here makes
        # the passivity a property of the control flow rather than a promise in
        # prose that a later edit could quietly break.
        return read_account_usage_cache(name)
    store = _store_path(None, Path.home())
    creds_path = store / name / ".credentials.json"
    if not creds_path.is_file():
        return read_account_usage_cache(name)
    if refresh:
        cache_path = _per_account_usage_cache_path(name)
        # stx-allow: fallback (reason: best-effort cache bust; if the
        # file is already gone or locked, the next call still re-fetches
        # because the cache reader gracefully returns None.)
        try:
            cache_path.unlink(missing_ok=True)
        except OSError:
            pass
    # stx-allow: fallback (reason: fetch_usage_for_credentials is documented never-raise, but defence-in-depth so one bad row never crashes `account list`)
    try:
        result = fetch_usage_for_credentials(creds_path)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return read_account_usage_cache(name)
    if result.get("error") or result.get("used_pct_5h") is None:
        cached = read_account_usage_cache(name)
        return cached if cached else None
    return result


# ---------------------------------------------------------------------------
# Identity verification
# ---------------------------------------------------------------------------


def verify_stored_identities(accounts: list[dict], *, opener=None) -> dict:
    """Resolve, for every stored account, WHOSE credential it actually holds.

    Returns ``{name: AccountIdentity}``. The store keeps no identity claim
    inside a credential, so until this runs the directory name is the only
    thing naming the account — and a directory name is not evidence. See
    :mod:`.._account.account_verify` for the incident this guards.

    Duplicate marking happens across the whole set at once, because a
    single account cannot know that another directory holds the same
    Anthropic account.
    """
    from .._account.account_verify import mark_duplicates, verify_account
    from .._state.account_store import _store_path

    store = _store_path(None, Path.home())
    identities = [
        verify_account(
            acct["name"],
            store / acct["name"] / ".credentials.json",
            claimed_email=acct.get("email_address"),
            opener=opener,
        )
        for acct in accounts
    ]
    return {ident.name: ident for ident in mark_duplicates(identities)}


# ---------------------------------------------------------------------------
# Row / JSON orchestrators
# ---------------------------------------------------------------------------


def build_stored_rows(
    accounts: list[dict],
    *,
    refresh: bool = False,
    opener=None,
    passive: bool = False,
    host: str = "",
) -> list[AccountRow]:
    """Convert stored-account dicts into :class:`AccountRow` for rendering.

    Pulls credential freshness (live recompute from ``expiresAt`` on every
    call), the account's VERIFIED identity, and usage% (cached or re-fetched
    depending on ``refresh``). Also carries through the per-window
    ``reset_at_5h`` / ``reset_at_7d`` so the usage-bars block can render the
    inline reset hint (gripe #2 of 2026-06-09; moved from the table cells
    onto the bars by the 2026-07-11 dedupe directive). Plan/tier are no
    longer resolved here — no human surface renders them (the JSON path
    keeps them via :func:`build_stored_json`).

    The identity check GATES the percentages: a usage figure read with a
    credential that turns out to belong to a different account is not this
    account's usage, however freshly it was fetched, so
    :func:`._account_usage_state.classify_usage` collapses it to ``unknown``
    and ``used_pct_*`` is dropped rather than displayed under the wrong name.
    """
    from .._account.creds_sync import account_freshness
    from ._account_list_render import AccountRow
    from ._account_usage_state import classify_usage

    identities = verify_stored_identities(accounts, opener=opener)
    rows: list[AccountRow] = []
    for acct in accounts:
        name = acct["name"]
        fresh = account_freshness(name)
        usage = usage_for_account(acct, refresh=refresh, passive=passive) or {}
        ident = identities.get(name)
        reading = classify_usage(usage, ident)
        rows.append(
            AccountRow(
                host=host,
                name=name,
                freshness_state=fresh.state,
                freshness_hours=fresh.hours,
                used_pct_5h=reading.pct_5h,
                used_pct_7d=reading.pct_7d,
                snapshot_as_of=reading.as_of,
                reset_at_5h=reading.reset_at_5h,
                reset_at_7d=reading.reset_at_7d,
                provider="claude-code",
                usage_state=reading.state,
                usage_age_seconds=reading.age_seconds,
                usage_reason=reading.reason,
                identity_state=ident.state if ident else "unverified",
                verified_email=ident.verified_email if ident else None,
                duplicate_of=ident.duplicate_of if ident else None,
            )
        )
    return rows


def build_stored_json(
    accounts: list[dict],
    *,
    refresh: bool = False,
    opener=None,
    passive: bool = False,
    host: str = "",
) -> list[dict]:
    """Enrich stored-account dicts for ``sac accounts list --json``.

    Each entry carries OFFLINE plan/tier, credential FRESHNESS
    (``state`` + signed hours), the per-account usage payload, and — new
    since the 2026-08-12 incident — the account's verified ``identity``.
    Timestamps remain ISO-8601 for JSON consumers; only the human renderer
    reformats.

    ``usage_state`` is emitted ALONGSIDE the raw ``usage`` payload rather
    than in place of it. Machine consumers keep the numbers they already
    parse, but can no longer read them without also being told whether sac
    stands behind them — a scripted capacity check that ignores the state
    field is now doing so visibly.
    """
    from .._account.creds_sync import account_freshness
    from .._state.account_store import read_account_plan
    from ._account_usage_state import classify_usage

    identities = verify_stored_identities(accounts, opener=opener)
    stored: list[dict] = []
    for acct in accounts:
        entry = dict(acct)
        name = acct["name"]
        entry["provider"] = "claude-code"
        entry["qualified_id"] = f"claude-code:{name}"
        # WHICH MACHINE this credential lives on. Empty on the historical
        # single-host path (nothing there had a second machine to disambiguate
        # from); the fleet view stamps it, because a credential is a per-host
        # FILE and the same account is routinely valid here and expired there.
        if host:
            entry["host"] = host
        entry.update(read_account_plan(name))
        fresh = account_freshness(name)
        entry["freshness"] = fresh.state
        entry["freshness_hours"] = fresh.hours
        usage = usage_for_account(acct, refresh=refresh, passive=passive)
        entry["usage"] = usage
        ident = identities.get(name)
        reading = classify_usage(usage or {}, ident)
        entry["usage_state"] = reading.state
        entry["usage_age_seconds"] = reading.age_seconds
        entry["usage_unknown_reason"] = reading.reason
        entry["identity"] = {
            "state": ident.state if ident else "unverified",
            "claimed_email": ident.claimed_email if ident else None,
            "verified_email": ident.verified_email if ident else None,
            "verified_uuid": ident.verified_uuid if ident else None,
            "duplicate_of": ident.duplicate_of if ident else None,
        }
        stored.append(entry)
    return stored


# ---------------------------------------------------------------------------
# OpenAI / cross-provider projections
# ---------------------------------------------------------------------------


def openai_account_name(meta: dict) -> str:
    """Derive the stable, human account slug used in provider-qualified IDs."""
    source = (
        meta.get("gateway_alias")
        or meta.get("email_address")
        or meta.get("account_id")
        or "active"
    )
    slug = re.sub(r"[^a-z0-9]+", "-", str(source).lower()).strip("-")
    return slug or "active"


def build_openai_row(meta: dict) -> "AccountRow | None":
    """Project the active Codex login into the provider-aware account table."""
    from ._account_list_render import AccountRow

    if not meta:
        return None
    return AccountRow(
        name=openai_account_name(meta),
        provider="openai",
        freshness_state="CONFIGURED",
        freshness_hours=None,
        used_pct_5h=None,
        used_pct_7d=None,
        snapshot_as_of=meta.get("last_refresh"),
    )


def build_openai_rows(accounts: list[dict]) -> list[AccountRow]:
    """Project all gateway-configured Codex logins into account rows."""
    return [row for meta in accounts if (row := build_openai_row(meta)) is not None]


def build_provider_accounts_json(
    stored: list[dict], openai_meta: dict | list[dict]
) -> list[dict]:
    """Build the collision-free cross-provider identity list for JSON users."""
    accounts = [dict(item) for item in stored]
    openai_accounts = openai_meta if isinstance(openai_meta, list) else [openai_meta]
    for meta in openai_accounts:
        if not meta:
            continue
        name = openai_account_name(meta)
        accounts.append(
            {
                "provider": "openai",
                "name": name,
                "qualified_id": f"openai:{name}",
                "active": True,
                "metadata": dict(meta),
            }
        )
    return accounts


__all__ = [
    "build_openai_row",
    "build_openai_rows",
    "build_provider_accounts_json",
    "build_stored_json",
    "build_stored_rows",
    "openai_account_name",
    "usage_for_account",
    "verify_stored_identities",
]
