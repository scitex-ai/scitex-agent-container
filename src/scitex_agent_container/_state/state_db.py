"""The ``state_db`` facade — two id helpers and the accessor re-exports.

THIS MODULE NO LONGER OPENS A DATABASE. It was the per-agent ``state.db``
engine — connection factory, ``init_schema``, ``open_db``, ``table_counts``
and the ``KNOWN_TABLES`` whitelist every generic reader walked — and all of
it is gone. sac's state is the per-host PostgreSQL store reached through
``scitex_dev.store``; ADR-0022 is the ruling and ADR-0023 records the one
place plain PostgreSQL was chosen over the store primitive.

The engine's last day is worth one paragraph, because the shape it ended in
is the argument for deleting it rather than leaving it. ``KNOWN_TABLES`` had
already shrunk to an EMPTY tuple and both schema constants to pure SQL
comments, so ``init_schema`` issued no DDL at all,
``open_db`` handed out a connection to a database with no tables, and
``table_counts`` could only ever return ``{}``. Nothing in ``src/`` called
any of the three. What remained was a module that created an empty file on
disk and a set of verbs that answered every question with a plausible zero —
the exact success-shaped wrong answer each departing table was removed to
avoid, one level up, in the accessor that was supposed to be the safe one.

``DEFAULT_DB_PATH`` WENT LAST, AND IT IS WHY THIS PARAGRAPH EXISTS. The engine
deletion left the PATH behind — a ``Path`` naming a ``state.db`` that nothing
created and nothing could open — on the stated ground that retiring it was "a
separate change with a separate argument", roughly fifty test modules having
saved and restored it as isolation ceremony. This is that change. The argument
is the one the constant's own comment made against itself: it selected no
storage, so the ~4900 per-test rebindings isolated nothing, and the test
sandbox's state-floor check spent a third of its sentinels proving a constant
nobody reads still pointed somewhere harmless. A guard that cannot fail costs
exactly what it appears to buy.

WHAT STAYED, AND WHY EACH IS HERE
=================================
:func:`now_iso` and :func:`new_uuid7` are pure value helpers with callers all
over the package; they never touched a connection. ``_resolve_host`` is
re-exported for the seven modules that import it from here. The four accessor
groups below are re-exported for the same reason — they moved into sibling
modules under the per-file line cap years before the storage moved, and every
``from ...state_db import X`` call site still resolves through this name.

The accessors themselves each own their storage now: :mod:`state_db_diary`
(``turns`` / ``errors`` / ``heartbeats``), :mod:`state_db_instances`
(``instances``), :mod:`state_db_gc` and :mod:`state_db_export`. This module
mediates nothing between a caller and a store — it is a namespace, and the
next reader should not have to open it to find that out.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

# Re-exported for the 7 modules that ``from ...state_db import
# _resolve_host`` (claude_session, _node_channel, state_db_export,
# state_db_gc, _send, send_cmds, _dispatch).
from .state_db_hostname import resolve_host as _resolve_host  # noqa: F401


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
from .state_db_instances import (  # noqa: E402,F401
    last_known_instance,
    list_active_instances,
    record_instance_start,
    record_instance_stop,
)
