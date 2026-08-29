#!/usr/bin/env python3
"""WHERE this host's rows land, and WHETHER they may land there.

Split out of ``migrate_channel_events_to_postgres.py`` when the provenance
correction pushed that file past the per-file line cap. It holds the two
questions that are about PLACEMENT rather than about copying rows:

* :func:`offset_for` — where do THESE rows already sit, or where must they go?
* :func:`refusals` — is putting them there safe, or would it strand a live
  consumer's cursor?

The copying itself, the argv, the verification and the operator output stay in
the script.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import _channel_import_provenance as prov


def group_by_target(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict]]:
    """The source rows, per target, each list sorted by the source's own id."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["target"]), []).append(dict(row))
    for entries in grouped.values():
        entries.sort(key=lambda r: int(r["id"]))
    return grouped


def offset_for(conn: Any, *, target: str, entries: list[dict]) -> tuple[int, bool]:
    """``(offset, already_imported)`` for this host's rows on this target.

    THE PROBE IS BY CONTENT, NOT BY POSITION, and that is a correction. The
    first version asked "is the row sitting at the source's own top id
    mine?" — a question that is only valid when the previous run applied
    offset 0. For a RELOCATED target the previous run shifted this host's
    rows above an earlier residency, so the probe landed on the EARLIER
    host's row, saw a mismatch, and concluded "not mine". It then returned
    the (now higher) ``pg_max``, so ``ON CONFLICT (target, id) DO NOTHING``
    had no id to conflict on and the whole history was inserted AGAIN at
    fresh ids — one extra copy per invocation, with the script's verification
    unable to see it because it windows on the shifted range it just wrote.

    Asking WHERE THIS ENVELOPE ALREADY SITS answers the same question
    without assuming the answer. A match yields the offset the previous run
    used, so the re-import conflicts on every id and moves nothing.

    The candidate is confirmed against the source's FIRST row as well as its
    last: ``meta_json`` is not unique by construction (the at-least-once
    retry path can duplicate an envelope), so a lookalike top row alone must
    not be allowed to imply an offset for the whole run.

    ``already_imported`` distinguishes "we have been here before" from "these
    ids belong to somebody else", which is what :func:`refusals` needs to tell
    a relocation apart from a daemon that has moved on. It is ALSO what makes
    the provenance backfill work: a re-run of an already-imported host records
    the window this returns without moving a row.
    """
    pg_max = int(
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM sac_channel_events WHERE target = %s",
            (target,),
        ).fetchone()[0]
    )
    if pg_max == 0:
        return 0, False

    first, top = entries[0], entries[-1]
    landed_at = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM sac_channel_events "
            "WHERE target = %s AND meta_json = %s ORDER BY id",
            (target, top["meta_json"]),
        ).fetchall()
    ]
    for landed in landed_at:
        offset = landed - int(top["id"])
        if offset < 0:
            continue
        anchor = conn.execute(
            "SELECT meta_json FROM sac_channel_events WHERE target = %s AND id = %s",
            (target, int(first["id"]) + offset),
        ).fetchone()
        if anchor is not None and anchor[0] == first["meta_json"]:
            return offset, True
    return pg_max, False


def newer_rows_than_source(
    conn: Any, *, target: str, entries: list[dict]
) -> tuple[int, int]:
    """``(unattributed, attributed)`` rows this target holds that POSTDATE it.

    The split is the whole fix. ``attributed`` rows sit inside an id window
    some import CLAIMED, so they are another host's history — which this
    script has always said it shifts above. ``unattributed`` rows are the ones
    nothing can account for, and only those can be a daemon that served in the
    deploy gap.
    """
    newest = max(float(row["ts"]) for row in entries)
    return prov.newer_rows(conn, target=target, newest_ts=newest)


def refusals(
    conn: Any,
    grouped: dict[str, list[dict]],
    *,
    accepted: frozenset[str] = frozenset(),
    accepted_replay: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """``(refused, waived)`` — targets this migration MUST NOT touch.

    THE ORDERING THIS ENFORCES: run the one-shot BEFORE the new code starts
    serving, per target. ``init_channel_schema`` creates the tables lazily on
    first connect, so the ordinary deploy sequence — restart ``sac listen``
    with the new code, then run the one-shot — has the daemon minting ids from
    1 for any target that receives a message in the gap. Those rows are
    genuinely new, and shifting the migrated history ABOVE them produces
    exactly the failure this design exists to prevent: every SQLite-era
    ``Last-Event-ID`` resolves to a DIFFERENT event, and the post-cutover rows
    sit BELOW every live cursor, unreachable through ``id > cursor`` forever.

    THE DISCRIMINATOR IS PROVENANCE, NOT TIME, and that is a correction. Time
    alone answers "could this row belong to an older residency?" — a proxy for
    the real question, and a proxy that FAILS whenever two hosts served one
    target over overlapping periods, which is the shape this fleet is in.
    Measured 2026-08-28: compute-03's import was refused by 152 rows of
    compute-04's already-imported history, with ZERO post-cutover writes among
    them, and no operator action could clear it — stopping ``sac listen`` does
    not remove rows that are already in the table.

    So a row is evidence of a live daemon only if NO RECORDED IMPORT CLAIMS
    IT. A daemon row minted in the deploy gap belongs to no window, is newer
    than the source, and is still refused; that is what the negative controls
    in ``test_migrate_channel_events_overlap.py`` pin, and they are the tests
    that matter most here.

    A target we have ALREADY imported is exempt: its own rows are trivially
    not newer than themselves, and a re-run must stay a no-op.

    ``accepted`` is the operator asserting, PER NAMED TARGET, that the
    unattributed rows above them are an import whose ``state.db`` is gone.
    ``accepted_replay`` is a DIFFERENT assertion for a different fact: those
    rows really are live daemon traffic, and the operator accepts the named,
    bounded consequence of importing anyway. There is deliberately no blanket
    ``--force``: a bypass covering every target at once restores exactly the
    corruption this guard exists to prevent, and this guard has already proved
    its worth by refusing rather than corrupting.

    THE TWO WAIVERS ARE NOT INTERCHANGEABLE, and keeping them apart is the
    point of having two. They assert opposite things about the same rows —
    "this is another host's history" versus "this is live traffic and I accept
    a replay" — and only one of them can be true. Two flags that both mean
    "proceed anyway" collapse into one habit, and the habit is what a guard
    cannot survive. So each is refused when its own precondition does not
    hold, rather than being quietly ignored.

    MOOT IS NOT THE SAME AS UNNECESSARY, and merging them broke the script's
    own contract. A waiver named for a target this run has ALREADY IMPORTED is
    MOOT: nothing is blocking it because the work is done, and refusing there
    made re-running the exact command that succeeded exit 1 on an
    already-correct state — indistinguishable from real failure to any retry
    wrapper or runbook, and a direct contradiction of RE-RUNNING IS SAFE in the
    script's module docstring. That case is now a NOTE and a no-op.

    A waiver named for a target that was never blocked at all is UNNECESSARY
    and is still REFUSED. That is the habit-forming case the two-flag design
    exists to prevent: an override passed where no override was ever needed is
    how "pass the flag anyway" becomes reflex.

    WHY THIS STAYS CONSERVATIVE, deliberately. The guard cannot tell whether
    a shift would actually strand anything, because that depends on where live
    CONSUMERS sit and there is no server-side record of consumer position:
    ``list_since_id`` takes the id from the request and ``Last-Event-ID`` is a
    client-side variable. Do not "fix" the imprecision — the only way to make
    this predicate exact is information the system does not have, and every
    attempt to sharpen it ends in a guard that cannot refuse.
    """
    refused: list[str] = []
    waived: list[str] = []
    named = set(accepted_replay) | set(accepted)
    # Named-but-not-needed splits in two, and collapsing them cost the
    # documented command its idempotence. See the MOOT/UNNECESSARY note below.
    moot: set[str] = set()
    unnecessary = set(named)
    for target in sorted(grouped):
        entries = grouped[target]
        _, already = offset_for(conn, target=target, entries=entries)
        if already:
            if target in named:
                unnecessary.discard(target)
                moot.add(target)
            continue
        unclaimed, claimed = newer_rows_than_source(
            conn, target=target, entries=entries
        )
        if not unclaimed:
            continue
        unnecessary.discard(target)
        if target in accepted and target in accepted_replay:
            refused.append(
                f"{target}: named to BOTH --accept-imported-history and "
                f"--accept-post-cutover-replay. Those assert opposite things "
                f"about the same {unclaimed} row(s) — another host's history, "
                f"or live daemon traffic — and at most one can be true. "
                f"Decide which, and pass only that one."
            )
            continue
        if target in accepted:
            waived.append(
                f"{target}: {unclaimed} unattributed row(s) ACCEPTED as "
                f"imported history on the operator's explicit assertion"
            )
            continue
        if target in accepted_replay:
            count, lo, hi, undelivered = prov.unattributed_span(
                conn,
                target=target,
                newest_ts=max(float(r["ts"]) for r in entries),
            )
            waived.append(
                f"{target}: ACCEPTING A REPLAY. {count} post-cutover row(s) at "
                f"ids {lo}..{hi} ({undelivered} undelivered) stay exactly where "
                f"they are — this host's {len(entries)} row(s) are offset ABOVE "
                f"them, so nothing becomes unreachable. THE COST YOU ARE "
                f"ACCEPTING: a consumer sitting at Last-Event-ID {hi} will "
                f"receive this host's {len(entries)} row(s) as if new."
            )
            continue
        seen = (
            ""
            if not claimed
            else (
                f" ({claimed} further newer row(s) ARE accounted for by a "
                f"recorded import, and were not held against this run)"
            )
        )
        refused.append(
            f"{target}: {unclaimed} row(s) already in the store are NEWER than "
            f"anything in this state.db and carry NO import provenance{seen}, "
            f"so this script cannot tell another host's history from live "
            f"daemon traffic. Do ONE of: (1) if those rows came from ANOTHER "
            f"HOST's state.db, re-run this with --commit --db-path pointing at "
            f"THAT file — it moves no rows and records the window it "
            f"recognises, after which this refusal CLEARS BY ITSELF; (2) if "
            f"`sac listen` really has served {target!r} since the cutover, "
            f"those rows are live traffic and STOPPING THE DAEMON WILL NOT "
            f"CLEAR THIS — they are already written and nothing removes them; "
            f"stopping it only prevents MORE, and you then accept the named "
            f"consequence with --accept-post-cutover-replay {target}; (3) if "
            f"they are an import whose state.db is GONE, assert that instead "
            f"with --accept-imported-history {target}."
        )
    for target in sorted(moot):
        waived.append(
            f"{target}: ALREADY IMPORTED, so the waiver flag was not needed "
            f"and did nothing. This run is a no-op for it. Re-running is safe "
            f"— the flag can stay in the command or be dropped, either way."
        )
    for target in sorted(unnecessary):
        refused.append(
            f"{target}: named to a waiver flag, but nothing is blocking it — "
            f"it has no rows newer than this state.db, so there is nothing to "
            f"waive. That is not a case an override exists for. Drop the flag "
            f"for {target!r} and re-run."
        )
    return refused, waived


def dry_run_refusals(
    grouped: dict[str, list[dict]],
    *,
    accepted: frozenset[str],
    accepted_replay: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """:func:`refusals` for the dry run — READ-ONLY, and never fatal.

    IT MUST NOT RUN THE DDL. ``new_channel_connection`` applies the schema on
    open, so reaching for it here would make the dry run CREATE the two
    tables — a write, in the mode whose entire contract is that it writes
    nothing. It connects raw instead and treats an absent table as "nothing
    to refuse", which is exactly true: a store with no rows cannot have moved
    on past this import.

    A store that cannot be opened is SAID OUT LOUD and skipped rather than
    reported as "nothing refused" — the reading that would make a clean dry
    run mean nothing. Same shape as ``_migrate_lib._preview_collisions``.
    """
    try:
        import psycopg

        from scitex_agent_container._state.state_db_channel_store import (
            _resolve_target,
        )

        conn = psycopg.connect(str(_resolve_target().dsn), autocommit=True)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"  (ordering check SKIPPED — cannot open the store: {exc!r})")
        return [], []
    try:
        exists = conn.execute(
            "SELECT to_regclass('sac_channel_events') IS NOT NULL"
        ).fetchone()[0]
        if not exists:
            return [], []
        return refusals(
            conn, grouped, accepted=accepted, accepted_replay=accepted_replay
        )
    finally:
        conn.close()
