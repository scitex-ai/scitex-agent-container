"""Protocol-backed outcome for one exact incarnation stop."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from typing import Any, Mapping

from scitex_dev.status import StatusCode, ledger_record, ledger_schema, new_exchange_id

_OWNERSHIP_FIELDS = (
    "process_start_time",
    "process_uid",
    "control_group",
    "scope_unit",
    "scope_invocation_id",
)


class StopVerificationError(RuntimeError):
    """The exact incarnation could not be proven absent."""

    def __init__(self, message: str, *, exchange_id: str) -> None:
        super().__init__(message)
        self.exchange_id = exchange_id


def _actor() -> str:
    return os.environ.get("SAC_NAME", "cli")


def _participant(host: str, name: str, incarnation_id: str) -> str:
    return f"{host}/scitex-agent-container/{name}/{incarnation_id}"


def has_complete_scope_ownership(instance: Mapping[str, Any] | None) -> bool:
    """Whether an incarnation carries every launch-recorded ownership fact."""
    return bool(
        instance is not None
        and all(instance.get(field) not in (None, "") for field in _OWNERSHIP_FIELDS)
    )


def record_stop_outcome(
    *,
    name: str,
    instance: Mapping[str, Any],
    status: StatusCode,
    exchange_id: str,
    opened_at: str,
    store: Any | None = None,
) -> None:
    """Write the final process outcome to scitex-dev's central ledger."""
    host = str(instance["host"])
    incarnation_id = str(instance["id"])
    row = ledger_record(
        exchange_id=exchange_id,
        initiator=f"{socket.gethostname()}/{_actor()}",
        responder=_participant(host, name, incarnation_id),
        operation="agent.stop",
        status=status,
        opened_at=opened_at,
    )
    owned_store = store is None
    if store is None:
        from scitex_dev.store import Store, WriterPolicy, host_store

        schema = ledger_schema()
        store = Store(
            host_store(pkg="scitex_dev", name=schema.name),
            schema,
            node=socket.gethostname(),
            writer_policy=WriterPolicy.MULTI_WRITER,
            actor="scitex-agent-container",
        )
    try:
        from scitex_dev.store import NEW_RECORD

        store.put(row, expected_revision=NEW_RECORD)
    finally:
        if owned_store:
            store.close()


def verify_tui_incarnation_stopped(
    *,
    name: str,
    instance: Mapping[str, Any] | None,
    runtime_stop_succeeded: bool,
    runtime: Any,
    config: Any,
    ensure_scope_down: Any = None,
    outcome_recorder: Any = record_stop_outcome,
) -> str:
    """Return exchange id only after exact-incarnation disappearance."""
    exchange_id = new_exchange_id()
    opened_at = datetime.now(timezone.utc).isoformat()
    if instance is None:
        raise StopVerificationError(
            f"stop of {name!r} REFUSED: no central instances incarnation could "
            f"be resolved, so process ownership is UNKNOWN. Probe with "
            f"`sac agents status {name} --json`.",
            exchange_id=exchange_id,
        )
    if not has_complete_scope_ownership(instance):
        missing = ", ".join(
            field for field in _OWNERSHIP_FIELDS if instance.get(field) in (None, "")
        )
        message = (
            f"stop of {name!r} REFUSED for incarnation {instance['id']}: "
            f"UNVERIFIED LEGACY OWNERSHIP is missing launch-recorded fields "
            f"({missing}); no process signal is authorized. Probe with "
            f"`sac agents status {name} --json`. Exchange: {exchange_id}."
        )
        outcome_recorder(
            name=name,
            instance=instance,
            status=StatusCode(kind="process", code=3, message=message),
            exchange_id=exchange_id,
            opened_at=opened_at,
        )
        raise StopVerificationError(message, exchange_id=exchange_id)
    if ensure_scope_down is None:
        from .._runners._scope_ownership import ensure_owned_scope_down

        ensure_scope_down = ensure_owned_scope_down

    stopped = bool(ensure_scope_down(instance))
    if stopped:
        outcome_recorder(
            name=name,
            instance=instance,
            status=StatusCode(
                kind="process",
                code=0,
                message=(
                    f"OBSERVED: incarnation {instance['id']} has no surviving "
                    f"launch PID or owned cgroup processes; verify with "
                    f"`sac agents status {name} --json`."
                ),
            ),
            exchange_id=exchange_id,
            opened_at=opened_at,
        )
        return exchange_id

    message = (
        f"stop of {name!r} FAILED for incarnation {instance['id']}: its exact "
        f"launch ownership did not reach an observed terminal state. Probe "
        f"with `systemctl --user status {instance.get('scope_unit', '<unknown-scope>')}` "
        f"and `sac agents status {name} --json`. Exchange: {exchange_id}."
    )
    outcome_recorder(
        name=name,
        instance=instance,
        status=StatusCode(kind="process", code=3, message=message),
        exchange_id=exchange_id,
        opened_at=opened_at,
    )
    raise StopVerificationError(message, exchange_id=exchange_id)


__all__ = [
    "StopVerificationError",
    "has_complete_scope_ownership",
    "record_stop_outcome",
    "verify_tui_incarnation_stopped",
]
