"""Legacy registry import for the instance store.

THE CROSS-HOST WIRE PAIR WAS DELETED ON 2026-08-29 — ``EXPORT_SCHEMA_VERSION``,
``export_state``, ``import_state`` and the ``--since`` filter map behind them,
along with the ``sac db export`` / ``sac db import`` commands they backed.
They shipped a JSON delta of :data:`KNOWN_TABLES` from one host's state.db to
another's, and every table they ever carried now lives in the shared
PostgreSQL store where each host reads and writes THE SAME ROWS. There is no
peer left to converge with, so a round trip could only re-insert a stale copy
of what the far side already holds. THE STORE IS THE SYNC.

What remains is :func:`import_legacy_registry`, which lifts the JSON shards a
pre-migration registry left on disk into ``instances``. It is a one-shot carrier
for operator data that predates the migration, not a sync path, and it writes
through the PostgreSQL instance store like every other writer — see its own
docstring for why ``db_path`` is accepted and ignored.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from .._env import getenv as _sac_env


def import_legacy_registry(
    registry_dir: Path,
    db_path: Path | None = None,
    host: str | None = None,
) -> dict[str, int]:
    """Lift the JSON files under ``registry_dir`` into ``instances``.

    Each JSON shard becomes one ``instances`` record marked
    ``exit_reason='reboot-swept'`` with ``ended_at`` = now. Idempotent:
    shards matching an existing record on ``(name, host, started_at)`` are
    skipped.

    Returns ``{"imported": N, "skipped": M}``.

    ``db_path`` IS ACCEPTED AND IGNORED, deliberately. ``instances`` moved to
    the shared PostgreSQL store on 2026-08-28 and this writer moved with it,
    but the parameter stays in the signature because ``sac db
    import-legacy-registry`` still threads a ``--db-path`` through and a
    ``TypeError`` at the CLI boundary would be a worse answer than a
    parameter that no longer selects anything. It is named here rather than
    left as a silent no-op.

    THE DEDUPE KEY IS ``(name, host, started_at)``, NOT the record identity.
    That is unchanged from the previous implementation and it has to be: the identity
    is ``(id, host)`` and ``id`` is MINTED here, so every re-run would mint a
    fresh one and every re-run would import everything again. The natural key
    is what makes this idempotent; the surrogate id never could.

    ``ended_at``/``exit_reason`` are written IN THE SAME PUT as the rest.
    They are IMMUTABLE fields and the store freezes such a field at its first
    stamped value, so a two-step "insert then tombstone" would work exactly
    once and then be unrepeatable. One put, one stamp.
    """
    from scitex_dev.store import NEW_RECORD

    from .state_db import new_uuid7, now_iso
    from .state_db_instances import scan_instances
    from .state_db_instances_store import ACTOR, run_with_reconnect, strip_unset

    if host is None:
        host = _sac_env("HOST") or socket.gethostname().split(".")[0]

    imported = 0
    skipped = 0
    if not registry_dir.exists():
        return {"imported": 0, "skipped": 0}

    swept_at = now_iso()
    shards: list[dict] = []
    for path in sorted(registry_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (
            json.JSONDecodeError,
            OSError,
        ):  # stx-allow: fallback (reason: malformed shard tolerated)
            skipped += 1
            continue
        if not (data.get("name") and data.get("started_at")):
            skipped += 1
            continue
        shards.append(data)

    if not shards:
        return {"imported": imported, "skipped": skipped}

    def _import(store) -> tuple[int, int]:
        seen = {
            (
                str(row.values.get("name")),
                str(row.values.get("host")),
                str(row.values.get("started_at")),
            )
            for row in scan_instances(store)
        }
        added = 0
        ignored = 0
        for data in shards:
            name = str(data["name"])
            started_at = str(data["started_at"])
            if (name, str(host), started_at) in seen:
                ignored += 1
                continue
            values = strip_unset(
                {
                    "name": name,
                    "pid": data.get("pid"),
                    "screen": data.get("screen"),
                    "workdir": data.get("workdir"),
                    "started_at": started_at,
                    "ended_at": swept_at,
                    "exit_reason": "reboot-swept",
                }
            )
            values["remote"] = False
            values["id"] = new_uuid7()
            values["host"] = str(host)
            store.put(values, expected_revision=NEW_RECORD, actor=ACTOR)
            seen.add((name, str(host), started_at))
            added += 1
        return added, ignored

    added, ignored = run_with_reconnect(_import)
    return {"imported": imported + added, "skipped": skipped + ignored}


__all__ = [
    "import_legacy_registry",
]
