"""SQLite-backed state for scitex-agent-container (F-CS11).

Replaces the per-agent JSON files under
``~/.scitex/agent-container/runtime/registry/`` with a single ``state.db``.

WHAT IT HOLDS TODAY IS ONE TABLE: ``channel_events`` (WI-1 durability).
:data:`KNOWN_TABLES` is the list, and it is the list every generic reader
walks — a list of one.

The WI-2 spawn DAG, ``lineage``, and the F-CS11 registry's ``instances``
were the other two until 2026-08-28. Both moved to the shared PostgreSQL
store (:mod:`.state_db_lineage_store` and :mod:`.state_db_instances`), and
for each an empty leftover would have been worse than a crash: every
reader treats "no ``lineage`` row for this child" as ROOT, and a root MAY
SPAWN; every reader treats an empty ``instances`` as "nothing is running",
which is what decides whether to start a SECOND copy of a live agent. See
the departure notes in :mod:`.state_db_schema`.

The F-CS11 registry was ``definitions`` / ``instances`` / ``events``, with
``instance_heartbeats`` (the legacy ``heartbeats`` time series, tied to an
``instances.id``) added in phase 2. ALL FOUR left on 2026-08-28:
``definitions`` and ``instance_heartbeats`` had no writer at all, ``events``
had no reader, and ``instances`` — the only one of the four that both a
writer and a reader ever reached — MOVED to the shared PostgreSQL store
(:mod:`state_db_instances`). :mod:`state_db_schema` carries a departure note
for each.

The single-file layout makes backup/sync trivial (one ``cp``). It also
kept the legacy ``actions.db`` table (``attempts``) co-located so queries
could join action history against instance lifecycle; that table left on
2026-08-28 — it never had a writer, so the join it promised had nothing
on one side. See :mod:`state_db_schema` for the departure note.

THE DIARY GROUP IS GONE FROM SQLite (2026-08-28)
================================================
``turns`` / ``errors`` / ``heartbeats`` were a third group here: each
agent appended rows like a journal and the lead read them back. The
WRITERS moved to per-host PostgreSQL first; this module was the residue
— the DDL that kept creating the three empty tables, and the
``KNOWN_TABLES`` entries that kept ``sac db show`` and ``sac db query``
reading them.

Empty is the dangerous shape. ``sac db show`` reporting ``turns 0``
while PostgreSQL holds the rows is not a missing feature, it is a WRONG
ANSWER that looks like a right one — the failure the ``incarnations``
removal named on 2026-08-19. So the names are removed rather than left
whitelisted, and asking for one now fails loudly instead of answering
zero. :mod:`state_db_diary` owns the trio end to end.

NOTE: this docstring used to promise that the rename of the original
F-CS11 ``heartbeats`` table to ``instance_heartbeats`` "STAYS: it is what
an old state.db still needs". It does not stay. ``instance_heartbeats``
itself left on 2026-08-28, so the migration's only effect on an old DB
would have been to re-create, under a name this schema no longer defines,
a table whose writer and reader are both deleted.

Large helper groups live in sibling modules, all re-exported from THIS
module so ``from ...state_db import X`` imports keep working:

  * :mod:`state_db_export` — import_legacy_registry.
  * :mod:`state_db_gc` — gc_dead_instances / _proc_btime.
  * :mod:`state_db_diary` — record_turn / record_error / record_heartbeat /
    latest_heartbeats_per_name. On PostgreSQL, NOT in this database.
  * :mod:`state_db_migrations` was here until 2026-08-28 — idempotent
    ``ALTER TABLE`` steps run on every ``init_schema``. It is DELETED, with
    its last function: ``migrate_instances_add_family_tree_cols`` ALTERed
    ``instances`` to add ``bound_port``/``remote``/``spawned_by``, and that
    table moved to the shared PostgreSQL store. Its two predecessors went
    earlier the same day with ``instance_heartbeats`` and
    ``node_comms_policy``.

    The module was kept for one commit as departure notes with no code, and
    that is the shape this package deletes rather than preserves: it had no
    importer, and its ``import sqlite3`` survived only to satisfy the SQLite
    freeze list. WHAT THE NOTES SAID, kept because one of them is a real
    ruling: the two heartbeat migrations could still FIRE on an old enough
    database, re-creating a table the schema had just declared it does not
    maintain — a migration whose success restores something the schema
    deleted is not a safety net. The others were permanent no-ops.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Re-exported for the 7 modules that ``from ...state_db import
# _resolve_host`` (claude_session, _node_channel, state_db_export,
# state_db_gc, _send, send_cmds, _dispatch). The ``instances`` CRUD
# that used it directly moved to state_db_instances; keep the
# re-export here so those import sites keep resolving.
from .._runtime_paths import runtime_base_dir
from .state_db_hostname import resolve_host as _resolve_host  # noqa: F401
# There is no ``state_db_migrations`` import here any more, because there is
# no such module — see the docstring above for what it did and why it went.
from .state_db_schema import (
    _SCHEMA_ACL,
    _SCHEMA_REGISTRY,
)

# ``SCITEX_AGENT_CONTAINER_STATE_DB`` still wins (explicit per-file
# override); its FALLBACK now routes through ``runtime_base_dir`` so the
# single ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` knob relocates state.db
# too. Unset env => identical to the historical
# ``~/.scitex/agent-container/runtime/state.db``.
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_STATE_DB",
        str(runtime_base_dir() / "state.db"),
    )
)


# Tables exposed by `sac db query --table=<t>`. Whitelisted so users
# can't pass arbitrary identifiers through str-format SQL.
KNOWN_TABLES = (
    # EMPTY. Every name this tuple ever carried left SQLite on 2026-08-28,
    # and ``instances`` — the LAST one, and the only table sac owned that
    # both a writer and a reader ever reached — was the last to go, to the
    # shared PostgreSQL store (:mod:`.state_db_instances`).
    #
    # A NAME LEFT HERE WOULD BE A WRONG ANSWER, NOT AN EMPTY ONE, and for
    # ``instances`` that reading is the worst of the set: ``sac db show``
    # would print ``instances 0`` while PostgreSQL holds the fleet's entire
    # lifecycle history, about the table an operator reaches for FIRST when
    # asking what is running. ``sac agents list`` is the verb that answers
    # that question now.
    #
    # AN EMPTY TUPLE IS THE HONEST SHAPE, and it is what every generic reader
    # should see: ``table_counts`` returns ``{}``, ``export_state`` /
    # ``import_state`` carry nothing, and ``sac db query --table`` can name
    # nothing. ``init_schema`` issues ZERO ``CREATE TABLE`` statements, so
    # there is no table for any of them to be right about.
    # ``lineage`` left on 2026-08-28, when the spawn DAG moved to the
    # shared PostgreSQL store (:mod:`.state_db_lineage_store`). Its DDL is
    # gone from :mod:`.state_db_schema`, which carries the departure note,
    # and its ``export_state`` filter is gone from
    # :mod:`.state_db_export` -- so keeping the name here would aim every
    # generic reader (``table_counts`` behind ``sac db show``,
    # ``export_state`` / ``import_state``, and the ``click.Choice`` for
    # ``sac db query``) at a table that no longer exists.
    #
    # The empty-is-dangerous argument this tuple has been shedding names
    # over is at its sharpest here, and it is worth naming once: an empty
    # ``lineage`` does not read as "wrong database", it reads as "every
    # agent is a ROOT" -- and a root may spawn. ``sac db show`` answering
    # ``lineage 0`` while the store holds the fleet's 23 edges would be a
    # wrong answer that looks like a right one about the table the whole
    # ACL is derived from.
    #
    # ``definitions``, ``instance_heartbeats`` and ``events`` left on
    # 2026-08-28, in one change, taking this tuple from six names to three.
    # Their DDL is gone from :mod:`.state_db_schema`, where each carries its
    # own departure note; what the three share is that EVERY reader of them
    # was generic — :func:`table_counts` behind ``sac db show``,
    # ``export_state`` / ``import_state``, and the ``click.Choice`` for
    # ``sac db query`` — i.e. every reader reached them through THIS tuple
    # and none of them through a name. That is what made the entries
    # load-bearing and what makes removing them the whole edit.
    #
    # They are not the same kind of dead, and the notes in the schema say
    # which is which: ``definitions`` and ``instance_heartbeats`` had no
    # writer (0 rows on every host measured), while ``events`` had two
    # writers and 1181 rows and no READER at all. A name left here would
    # therefore have failed differently in each case — a plausible zero for
    # the first two, and for ``events`` a table sac exports and counts and
    # lets an operator query while nothing in ``src/`` consults it. Both
    # readings are the success-shaped answer this tuple has been shedding
    # names to avoid all month.
    # ``node_tokens`` left on 2026-08-28 with the per-node bearer feature
    # it belonged to: ``mint_node_token`` had zero callers outside tests,
    # so the table was empty on every host and no bearer ever resolved to
    # a name. Its DDL is gone from :mod:`.state_db_schema`, so keeping the
    # name here would aim every generic reader -- ``table_counts`` behind
    # ``sac db show``, ``export_state`` / ``import_state``, and the
    # ``click.Choice`` for ``sac db query`` -- at a table that no longer
    # exists. It is also the entry that made ``sac db export`` ship a
    # column of BEARER SECRETS to any peer that asked, since export takes
    # whole tables and the MCP ``db_export`` tool cannot name a subset;
    # dropping the entry closes that by construction rather than by
    # remembering to filter. ``_store_plugin.NEVER_SYNCED`` deliberately
    # KEEPS its refusal of this name -- a table leaving this tuple must
    # not read as the refusal being withdrawn.
    # ``comms_nodes`` left on 2026-08-28 when the ADR-0014 cross-host
    # directory moved to the shared PostgreSQL store
    # (:mod:`.state_db_comms_nodes`). Removed rather than whitelisted for
    # the usual reason — a name with no table answers every generic reader
    # with a plausible zero — and for one that is specific to this table:
    # `sac db export --tables comms_nodes` was the transport `sac registry
    # sync` ran over ssh, so leaving the name here would have kept an
    # anti-entropy sweep shipping empty payloads between hosts and
    # reporting `inserted=0` as success. The store IS the sync now.
    # ``attempts`` left on 2026-08-28 under the same ruling, for the
    # simplest possible version of the reason: it never had a writer. Its
    # DDL is gone from :mod:`.state_db_schema`, so keeping the name here
    # would point every generic reader -- ``table_counts`` behind ``sac db
    # show``, ``export_state``/``import_state``, and the ``click.Choice``
    # for ``sac db query`` -- at a table that no longer exists.
    # ``comms_grants`` left on 2026-08-28 under the same ruling as
    # ``incarnations`` below. Its CRUD had already moved to the shared
    # PostgreSQL store (:mod:`.state_db_grants`, which resolves through
    # ``host_store``); only the DDL and this whitelist entry were left,
    # and the entry is what kept the GENERIC readers -- ``table_counts``
    # behind ``sac db show``, ``export_state``/``import_state``, and the
    # ``click.Choice`` for ``sac db query`` -- pointed at a SQLite table
    # nothing writes. The 52 live rows were carried into PostgreSQL
    # before this landed.
    # ``incarnations`` was here until 2026-08-19. It now lives in per-host
    # PostgreSQL via :mod:`.state_db_incarnations`, so it is NOT queryable
    # through `sac db query`. Removed rather than left behind: a whitelisted
    # name with no table returns an EMPTY result, and an empty result reads
    # as "this agent has no incarnations" when the truth is "you are asking
    # the wrong database". An unknown-table error is the honest answer.
    #
    # ``turns``, ``errors`` and ``heartbeats`` left on 2026-08-28 under the
    # SAME ruling, and the three go together because they share
    # :mod:`.state_db_diary` and the loops below. Every reader of this tuple
    # is generic — :func:`table_counts`, ``export_state``, ``import_state``,
    # and the ``--table`` choice list — so one name left behind here would
    # have kept `sac db show` printing ``turns 0`` while the rows sat in
    # PostgreSQL, and a half-migrated trio is a split brain that raises
    # nothing: some readers see a row, others do not.
)


def now_iso() -> str:
    """ISO-8601 UTC with trailing 'Z' (matches the legacy registry format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid7() -> str:
    """Return a uuid7 string (time-ordered, sortable by start time).

    Falls back to uuid4 if uuid.uuid7 is unavailable on the runtime
    Python (added in 3.14).
    """
    if hasattr(uuid, "uuid7"):
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    return str(uuid.uuid4())


def _default_connector(db_path: Path) -> sqlite3.Connection:
    """Default sqlite3 connection factory (test seam)."""
    return sqlite3.connect(db_path, timeout=30.0)


def _connect(
    db_path: Path,
    connector=_default_connector,
) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connector(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    current_mode = conn.execute("PRAGMA journal_mode").fetchone()
    if current_mode and str(current_mode[0]).lower() != "wal":
        import time

        for attempt in range(50):
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 49:
                    raise
                time.sleep(0.02 * (attempt + 1))
    # GPFS / network-FS reliability. WAL (above) alone still floods
    # "disk I/O error" on Spartan GPFS under the heartbeat write loop
    # (cohort-A 2026-06-24, a SINGLE run -- not the %16-concurrency
    # case). scitex-db runs SQLite on GPFS reliably (neurovista) with
    # the tunings below; mirror its recipe. The IOERR is on WRITES, so
    # the load-bearing ones are synchronous=NORMAL (WAL-safe, far fewer
    # GPFS fsyncs) and temp_store=MEMORY (no transient temp files on
    # GPFS); mmap_size + wal_autocheckpoint complete scitex-db's set.
    # Best-effort per-PRAGMA: a tuning that errors on an exotic FS must
    # not kill an otherwise-usable connection.
    for _pragma in (
        "PRAGMA synchronous = NORMAL",
        "PRAGMA temp_store = MEMORY",
        "PRAGMA mmap_size = 30000000000",
        "PRAGMA wal_autocheckpoint = 1000",
    ):
        try:
            conn.execute(_pragma)
        except sqlite3.Error:  # stx-allow: fallback (reason: tuning PRAGMAs are advisory; a failed one must not block open_db)
            pass
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(db_path: Path | None = None) -> Path:
    """Create state.db with all tables if missing. Idempotent.

    Returns the resolved database path.
    """
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    with _connect(path) as conn:
        # ``migrate_legacy_heartbeats`` and
        # ``migrate_instance_heartbeats_add_seq`` ran here until 2026-08-28.
        # ``instance_heartbeats`` left SQLite that day (zero callers on both
        # its writer and its reader, zero rows on every host), and both
        # migrations existed only to shepherd an old DB INTO that table —
        # one renaming the legacy ``heartbeats`` onto the name, the other
        # rebuilding it for a ``seq`` PK. Kept, they would have gone on
        # re-creating a table this schema no longer defines, on exactly the
        # old databases least able to explain where it came from.
        conn.executescript(_SCHEMA_REGISTRY)
        # ``migrate_instances_add_family_tree_cols`` ran here until
        # 2026-08-28. ``instances`` moved to PostgreSQL, so the migration
        # returns early on every host forever — a schema step that can never
        # fire is not a safety net, it is a claim that one still happens.
        # Deleted with the DDL.
        # The two ``node_comms_policy`` ADD COLUMN migrations ran here
        # until 2026-08-28. The table moved to PostgreSQL, so both would
        # now be permanent no-ops against a table SQLite no longer has —
        # dead code claiming a live purpose. Removed with the DDL.
        # ``_SCHEMA_ATTEMPTS`` ran here until 2026-08-28. The ``attempts``
        # table had zero writers, so issuing its DDL only produced an empty
        # table that answered readers with a plausible zero. Existing rows
        # are untouched — we stop issuing the CREATE, we do not DROP.
        # ``_SCHEMA_CHANNEL_AND_ACL`` became ``_SCHEMA_ACL`` on 2026-08-28
        # when ``channel_events`` -- the LAST SQLite table sac owned -- moved
        # to the shared PostgreSQL as ``sac_channel_events`` /
        # ``sac_channel_cursor`` (:mod:`.state_db_channel_store`). Same
        # ruling as the diary and ``attempts``: we stop issuing the CREATE,
        # we do not DROP, so an old state.db keeps its rows until
        # ``scripts/migrate_channel_events_to_postgres.py`` carries them over.
        conn.executescript(_SCHEMA_ACL)
        # ``turns`` / ``errors`` / ``heartbeats`` were created by the
        # constant above (then called ``_SCHEMA_DIARY``) until 2026-08-28.
        # All three moved to per-host PostgreSQL; each diary store creates
        # its own schema on first open (``state_db_diary._open``), so there
        # is nothing to run here for them.
        # Task #27's two ACL tables were both created here until 2026-08-20.
        # ``pending_prompts`` and ``comms_blocks`` have BOTH moved to per-host
        # PostgreSQL; each store creates its own schema on first open
        # (``state_db_pending_approval.open_pending_prompt_store`` /
        # ``state_db_blocks.open_blocks_store``), so there is nothing to run
        # here for either. ``comms_grants`` was the last of that pair left
        # in SQLite; it moved to the shared PostgreSQL store and its DDL
        # was deleted on 2026-08-28, so nothing of the pair remains here.
        # The ``incarnations`` birth-certificate table used to be created
        # here. It moved to per-host PostgreSQL on 2026-08-19; the promise
        # this comment block used to make — "lives in the EXISTING sqlite
        # factory ON PURPOSE so the separately-carded sqlite→Postgres
        # migration carries it along" — is now kept. Its schema is created
        # on first open by :func:`state_db_incarnations.open_incarnation_store`,
        # so there is nothing to run here.
        # sac-comms item D's rate-limit log (acl_deny_notify_log) was created
        # here until 2026-08-20. It moved to per-host PostgreSQL alongside the
        # two task-#27 tables above; its schema is created on first open by
        # ``state_db_acl_deny_notify.open_deny_notify_store``.
        conn.commit()
    return path


@contextmanager
def open_db(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed connection. Initialises schema on first use."""
    path = init_schema(db_path)
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_counts(db_path: Path | None = None) -> dict[str, int]:
    """Return ``{table_name: row_count}`` for every known table."""
    counts: dict[str, int] = {}
    with open_db(db_path) as conn:
        for table in KNOWN_TABLES:
            row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
            counts[table] = int(row["n"])
    return counts


# ``instances`` lifecycle CRUD (record_instance_start / _stop /
# list_active_instances) moved to :mod:`state_db_instances` under the
# per-file line cap; re-exported below so callers keep importing them
# from :mod:`state_db`.

# Re-export the helpers that used to live in this file but moved
# into sibling modules under the per-file line cap. Existing callers
# keep importing them from :mod:`state_db`.
from .state_db_diary import (  # noqa: E402,F401
    latest_heartbeats_per_name,
    record_error,
    record_heartbeat,
    record_turn,
)
from .state_db_export import import_legacy_registry  # noqa: E402,F401
from .state_db_gc import (  # noqa: E402,F401
    _proc_btime,
    gc_dead_instances,
)
# ``latest_instance_heartbeat`` and ``update_heartbeat`` were re-exported
# here from :mod:`state_db_heartbeats` until 2026-08-28. Neither had a
# single caller in ``src/`` — the re-export lines themselves were two of
# the three references the package held — so the module went with its
# table. See the ``instance_heartbeats`` departure note in
# :mod:`state_db_schema`, which also names what the deletion takes with
# it: ``instances.last_heartbeat_at`` and the three token/iter counters
# lose their only (already-uncalled) writer.
from .state_db_instances import (  # noqa: E402,F401
    last_known_instance,
    list_active_instances,
    record_instance_start,
    record_instance_stop,
)
