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

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Re-exported for the 7 modules that ``from ...state_db import
# _resolve_host`` (claude_session, _node_channel, state_db_export,
# state_db_gc, _send, send_cmds, _dispatch).
from .._runtime_paths import runtime_base_dir
from .state_db_hostname import resolve_host as _resolve_host  # noqa: F401

#: THE PATH IS ALL THAT IS LEFT OF ``state.db``, AND NOTHING CREATES IT.
#:
#: Kept for one reason only: the test sandbox's state-floor check
#: (``tests/conftest.py::_assert_state_floor_intact``) reads it, alongside
#: ``registry.REGISTRY_DIR`` and ``_session_state.DEFAULT_STATE_ROOT``, to
#: prove a suite has not pointed an import-time constant at the operator's
#: real runtime directory. It selects no storage: no code path in ``src/``
#: opens it, and with the engine deleted no code path can.
#:
#: Retiring the constant is a separate change with a separate argument —
#: roughly fifty test modules save/restore it as isolation ceremony that has
#: already stopped isolating anything — and doing it here would have hidden a
#: fifty-file fixture rewrite inside an engine deletion.
DEFAULT_DB_PATH = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_STATE_DB",
        str(runtime_base_dir() / "state.db"),
    )
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
