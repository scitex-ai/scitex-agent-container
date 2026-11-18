"""Host-authoritative heartbeat leases on ``instances`` rows.

Split out of :mod:`.state_store_instances` (per-file line cap); that module
re-exports every name here, so existing imports keep resolving. The rows are
the same shared PostgreSQL ``instances`` records; see that module's
docstring for the Store semantics (partial puts, scans by id).
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from .heartbeat_columns import (
    HEARTBEAT_COLUMNS,
    NULL_READS_AS,
    OPTIONAL_HEARTBEAT_FIELDS,
)
from .state_store_hostname import resolve_host as _resolve_host
from .state_store_instances_store import ACTOR, instance_key, run_with_reconnect

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store


# The field set, its columns and which fields are optional are declared ONCE
# in :mod:`.heartbeat_columns`, which both instances schemas also derive their
# heartbeat block from -- so every column written here is a declared column.
_HEARTBEAT_COLUMNS = HEARTBEAT_COLUMNS
_OPTIONAL_HEARTBEAT_FIELDS = OPTIONAL_HEARTBEAT_FIELDS


def _heartbeat_columns(validated: dict[str, object]) -> dict[str, object]:
    """Map one validated beat onto its ``instances`` columns.

    A required field that is missing still raises KeyError: validation
    guarantees it, so its absence here is a bug, not a beat.
    """
    return {
        column: (
            validated.get(field)
            if field in _OPTIONAL_HEARTBEAT_FIELDS
            else validated[field]
        )
        for field, column in _HEARTBEAT_COLUMNS.items()
    }


def _heartbeat_from_instance(values: dict) -> dict[str, object] | None:
    if not values.get("heartbeat_boot_id"):
        return None
    heartbeat: dict[str, object] = {
        "host": str(values.get("host") or ""),
    }
    for field, column in _HEARTBEAT_COLUMNS.items():
        value = values.get(column)
        heartbeat[field] = NULL_READS_AS.get(field) if value is None else value
    return heartbeat


def _assert_heartbeat_host_authority(row_host: str, canonical_host: str) -> None:
    from .authoritative_heartbeat import AuthoritativeHeartbeatError

    if row_host != canonical_host:
        raise AuthoritativeHeartbeatError(
            "host authority mismatch: this process cannot renew another host's lease"
        )


def record_instance_heartbeat(instance_id: str, heartbeat: dict[str, object]) -> bool:
    """Advance one active instance's host-owned lease; never insert or revive."""
    from datetime import datetime, timezone

    from .authoritative_heartbeat import AuthoritativeHeartbeatError, validate_heartbeat

    if not os.environ.get("SCITEX_STORE_DSN", "").strip():
        raise AuthoritativeHeartbeatError(
            "authoritative heartbeat requires SCITEX_STORE_DSN; per-host store "
            "fallback cannot provide fleet lease authority"
        )

    from .state_store_instances import find_by_id

    def _record(store: "Store") -> bool:
        row = find_by_id(store, instance_id)
        if row is None or row.values.get("ended_at") is not None:
            return False
        values = row.values
        row_host = str(values.get("host") or "")
        canonical_host = _resolve_host(None)
        _assert_heartbeat_host_authority(row_host, canonical_host)
        previous = _heartbeat_from_instance(dict(values))
        validated = validate_heartbeat(
            heartbeat,
            expected_agent=str(values.get("name") or ""),
            expected_host=row_host,
            now=time.time(),
            previous=previous,
        )
        new_seq = validated["seq"]
        observed_at = validated["observed_at"]
        if type(new_seq) is not int or not isinstance(observed_at, (int, float)):
            raise AuthoritativeHeartbeatError("validated heartbeat lost numeric fields")

        observed = datetime.fromtimestamp(
            float(observed_at), tz=timezone.utc
        ).isoformat()
        key = instance_key(values)
        revision = store.revision(key)
        store.put(
            {
                **key,
                "last_heartbeat_at": observed,
                "heartbeat_boot_id": validated["boot_id"],
                "heartbeat_seq": new_seq,
                "heartbeat_state": validated["state"],
                "lease_expires_at": validated["lease_expires_at"],
                **_heartbeat_columns(validated),
            },
            expected_revision=revision,
            actor=ACTOR,
        )
        return True

    return bool(run_with_reconnect(_record))


def latest_authoritative_heartbeats() -> list[dict[str, object]]:
    """Return federated current leases from the explicitly shared instance store."""
    from .authoritative_heartbeat import AuthoritativeHeartbeatError

    if not os.environ.get("SCITEX_STORE_DSN", "").strip():
        raise AuthoritativeHeartbeatError(
            "authoritative heartbeat reads require SCITEX_STORE_DSN"
        )

    from .state_store_instances import scan_instances

    def _read(store: "Store") -> list[dict[str, object]]:
        return _authoritative_heartbeats_from_rows(
            scan_instances(store), now=time.time()
        )

    return list(run_with_reconnect(_read))


def _authoritative_heartbeats_from_rows(
    rows: list[Any], *, now: float
) -> list[dict[str, object]]:
    """Validate instance-backed leases using Store HLC as receiver wall time."""
    from .authoritative_heartbeat import (
        select_federated_heartbeats,
        validate_heartbeat,
    )

    beats = []
    for row in rows:
        if row.values.get("ended_at") is not None:
            continue
        heartbeat = _heartbeat_from_instance(dict(row.values))
        if heartbeat is None:
            continue
        heartbeat_stamp = row.field_hlc.get("heartbeat_seq")
        if heartbeat_stamp is None:
            continue
        received_at = float(heartbeat_stamp.wall_us) / 1_000_000.0
        heartbeat["observed_at"] = received_at
        heartbeat["lease_expires_at"] = received_at + 90.0
        progress_at = heartbeat.get("progress_at")
        if isinstance(progress_at, (int, float)) and not isinstance(progress_at, bool):
            heartbeat["progress_at"] = min(float(progress_at), received_at)
        validated = validate_heartbeat(
            heartbeat,
            expected_agent=str(heartbeat["agent_id"]),
            expected_host=str(heartbeat["host"]),
            now=now,
        )
        process_alive = None
        if (
            str(heartbeat.get("host") or "") == _resolve_host(None)
            and not os.environ.get("APPTAINER_CONTAINER")
            and not os.environ.get("SINGULARITY_CONTAINER")
        ):
            pid = row.values.get("pid")
            if type(pid) is int and pid > 0:
                try:
                    os.kill(pid, 0)
                    process_alive = True
                except ProcessLookupError:
                    process_alive = False
                except (PermissionError, OSError):
                    process_alive = None
        validated["_process_alive"] = process_alive
        lease_value = validated["lease_expires_at"]
        validated["_federation_connected"] = bool(
            isinstance(lease_value, (int, float))
            and not isinstance(lease_value, bool)
            and float(lease_value) >= now
        )
        beats.append(validated)
    return select_federated_heartbeats(beats, now=now)
