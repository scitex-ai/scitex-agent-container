"""Retire identity-proven durable-inbox sidecars from older launches.

The Hermes inbox bridge was renamed to the harness-neutral channel dispatcher.
The replacement lifecycle used a new module name and pidfile, which made an
already detached legacy process invisible on upgrade.  This reconciler scans
process metadata instead of trusting either generation's pidfile.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

CURRENT_MODULE = "scitex_agent_container.runtimes._channel_inbox_dispatcher"
LEGACY_MODULE = "scitex_agent_container.runtimes._hermes_inbox_bridge"
CURRENT_ROLE = "channel-inbox-dispatcher"
LEGACY_ROLE = "hermes-inbox-bridge"
LEGACY_PID_FILENAME = "hermes-inbox-bridge.pid"
_ROLE_BY_MODULE = {CURRENT_MODULE: CURRENT_ROLE, LEGACY_MODULE: LEGACY_ROLE}


@dataclass(frozen=True)
class SidecarReceipt:
    """Auditable disposition for one exact process identity."""

    pid: int
    module: str
    process_role: str
    incarnation_id: str | None
    disposition: str
    reason: str
    signals: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    pid: int
    module: str
    role: str
    incarnation_id: str | None
    create_time: float | None


def _default_process_iter() -> Iterable[Any]:
    import psutil

    return psutil.process_iter(["pid", "cmdline", "environ", "create_time"])


def _info(proc: Any, key: str, default: Any = None) -> Any:
    try:
        info = getattr(proc, "info", None)
        if isinstance(info, dict) and key in info:
            return info[key]
        value = getattr(proc, key)
        return value() if callable(value) else value
    except Exception:  # stx-allow: fallback (reason: normal process-exit/access races are skipped)
        return default


def _value(argv: list[str], flag: str) -> str | None:
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _canonical(path: str) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _candidate(proc: Any, *, name: str, config_path: str) -> _Candidate | None:
    argv = [str(value) for value in (_info(proc, "cmdline", ()) or ())]
    modules = [module for module in _ROLE_BY_MODULE if module in argv]
    if len(modules) != 1:
        return None
    module = modules[0]
    inferred_role = _ROLE_BY_MODULE[module]
    declared_role = _value(argv, "--process-role")
    if declared_role is not None and declared_role != inferred_role:
        return None
    authored_path = _value(argv, "--config-path")
    if (
        _value(argv, "--name") != name
        or authored_path is None
        or _canonical(authored_path) != _canonical(config_path)
    ):
        return None
    env = _info(proc, "environ", {}) or {}
    incarnation = _value(argv, "--incarnation-id") or env.get("SAC_INSTANCE_UUID")
    try:
        pid = int(_info(proc, "pid"))
    except (TypeError, ValueError):
        return None
    created = _info(proc, "create_time")
    return _Candidate(
        pid=pid,
        module=module,
        role=inferred_role,
        incarnation_id=str(incarnation) if incarnation else None,
        create_time=float(created) if created is not None else None,
    )


def _snapshot(
    process_iter: Callable[[], Iterable[Any]], *, name: str, config_path: str
) -> list[_Candidate]:
    return [
        candidate
        for proc in process_iter()
        if (candidate := _candidate(proc, name=name, config_path=config_path))
        is not None
    ]


def reconcile_inbox_sidecars(
    *,
    name: str,
    config_path: str,
    incarnation_id: str | None,
    authoritative_pid: int | None = None,
    state_dir: Path | None = None,
    process_iter: Callable[[], Iterable[Any]] | None = None,
    signal_fn: Callable[[int, int], None] = os.kill,
    sleep_fn: Callable[[float], None] = time.sleep,
    grace_s: float = 0.5,
) -> tuple[SidecarReceipt, ...]:
    """Preserve one current owner and retire exact stale/legacy duplicates.

    A process is eligible only when module, agent name, and canonical authored
    config all match.  The current module additionally needs the current
    incarnation and authoritative pid to be preserved.  An injected launch
    harness may provide no incarnation; because prelaunch has no authoritative
    pid, that cannot preserve a process.  The retired legacy role is never a
    valid owner after upgrade.
    """
    iterator = process_iter or _default_process_iter
    initial = sorted(
        _snapshot(iterator, name=name, config_path=config_path),
        key=lambda item: item.pid,
    )
    keep = {
        item.pid
        for item in initial
        if item.module == CURRENT_MODULE
        and item.pid == authoritative_pid
        and item.incarnation_id == incarnation_id
    }
    stale = [item for item in initial if item.pid not in keep]
    receipts = [
        SidecarReceipt(
            pid=item.pid,
            module=item.module,
            process_role=item.role,
            incarnation_id=item.incarnation_id,
            disposition="preserved",
            reason="authoritative-current-incarnation",
        )
        for item in initial
        if item.pid in keep
    ]
    term_sent: set[int] = set()
    for item in stale:
        try:
            signal_fn(item.pid, signal.SIGTERM)
            term_sent.add(item.pid)
        except ProcessLookupError:
            pass
    if term_sent:
        sleep_fn(grace_s)
    remaining = {
        (item.pid, item.create_time): item
        for item in _snapshot(iterator, name=name, config_path=config_path)
    }
    for item in stale:
        signals = (signal.SIGTERM,) if item.pid in term_sent else ()
        # PID + create_time prevents escalation into a reused process.
        if (item.pid, item.create_time) in remaining:
            signal_fn(item.pid, signal.SIGKILL)
            signals += (signal.SIGKILL,)
        reason = (
            "retired-legacy-role"
            if item.role == LEGACY_ROLE
            else "stale-or-duplicate-current-role"
        )
        receipts.append(
            SidecarReceipt(
                pid=item.pid,
                module=item.module,
                process_role=item.role,
                incarnation_id=item.incarnation_id,
                disposition="retired",
                reason=reason,
                signals=signals,
            )
        )
    if any(receipt.signals[-1:] == (signal.SIGKILL,) for receipt in receipts):
        sleep_fn(0.05)
        survivors = {
            (item.pid, item.create_time)
            for item in _snapshot(iterator, name=name, config_path=config_path)
        }
        identity_by_pid = {item.pid: item.create_time for item in stale}
        failed = [
            receipt.pid
            for receipt in receipts
            if receipt.disposition == "retired"
            and (receipt.pid, identity_by_pid[receipt.pid]) in survivors
        ]
        if failed:
            raise RuntimeError(
                "identity-proven stale inbox sidecars survived SIGKILL: "
                + ", ".join(str(pid) for pid in failed)
            )
    if state_dir is not None:
        (Path(state_dir) / LEGACY_PID_FILENAME).unlink(missing_ok=True)
    for receipt in receipts:
        log.info("inbox sidecar reconcile: %s", receipt)
    return tuple(sorted(receipts, key=lambda receipt: receipt.pid))


__all__ = [
    "CURRENT_MODULE",
    "CURRENT_ROLE",
    "LEGACY_MODULE",
    "LEGACY_PID_FILENAME",
    "LEGACY_ROLE",
    "SidecarReceipt",
    "reconcile_inbox_sidecars",
]
