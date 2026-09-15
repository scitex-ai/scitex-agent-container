"""Refuse an explicit-engine start that would silently no-op on another engine."""

from __future__ import annotations

from typing import Any

from ..cli_pkg._health_engine import _read_running_engine

__all__ = ["ExplicitEngineNoopError", "assert_explicit_engine_noop_safe"]


class ExplicitEngineNoopError(RuntimeError):
    """A live process prevents SAC from applying the explicitly selected engine."""


def assert_explicit_engine_noop_safe(
    config: Any,
    requested_engine: str | None,
    *,
    force: bool,
    dry_run: bool,
    proc_root: Any = None,
) -> None:
    """Allow an idempotent no-op only when it proves the requested engine is live.

    ``sac agents start`` intentionally leaves a live process untouched unless
    ``--force`` is present. That contract cannot silently swallow
    ``--engine``: finding *some* live process does not prove that the selected
    backend was applied. Observe the live process using the same engine census
    as ``sac agents health``. A mismatch or an incomplete observation refuses
    without changing either the process or its conversation.
    """
    selected = str(requested_engine or "").strip()
    if not selected or force or dry_run:
        return

    resolved = str(getattr(config, "engine_key", "") or "").strip()
    running, scan, reason = _read_running_engine(
        str(config.name), proc_root=proc_root
    )
    if running == resolved:
        return

    if running:
        evidence = f"the live process reports engine {running!r}"
    else:
        census = scan.to_dict()
        evidence = (
            f"the live engine could not be proven ({reason or 'unknown reason'}; "
            f"matched={census['pids_matched']}, unreadable={census['pids_unreadable']}, "
            f"vantage={census['vantage']})"
        )
    raise ExplicitEngineNoopError(
        f"agent {config.name!r} is already running, so --engine {selected!r} "
        f"would be a no-op; {evidence}. No process or conversation was "
        "changed. To apply the selected engine while retaining Hermes session "
        f"history, run: sac agents start {config.name} --force --continue "
        f"--engine {selected} -y"
    )
