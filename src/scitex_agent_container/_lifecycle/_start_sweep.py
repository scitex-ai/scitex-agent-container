"""Self-cleaning instance sweep on the agent start path.

``sac store clean`` / ``sac store tick`` previously ran ONLY when an
operator (or a cron nobody installed — no timer ships for ``tick``)
invoked them, so dead ``instances`` rows accumulated until somebody
looked (Oct-10 20-min restart-loop residue; card
``infra-start-silent-noop-and-stale-rows-20261016``).

Every ``agent_start`` now runs the same host-scoped sweep best-effort
BEFORE the liveness verdict resolves, so the verdict reads post-sweep
state and a zombie lease can no longer vouch for a dead agent between
manual cleans. Cross-host (``remote``) rows are still never touched —
:func:`gc_dead_instances` scopes all three branches to this host by
construction, and this module does not widen that.

The sweep must never break a start: any exception folds to ``{}``.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["sweep_before_start"]

#: Injectable seam: ``Callable[[], dict]`` returning gc-style counters.
SweepFn = Callable[[], dict[str, Any]]


def _real_sweep() -> dict[str, Any]:
    # Lazy: ``_lifecycle._start`` <-> ``_state`` import order is load-
    # bearing; the stale-lease helper uses the same lazy pattern.
    from .._state.state_store import gc_dead_instances

    return gc_dead_instances()


def sweep_before_start(
    *,
    dry_run: bool = False,
    sweep_fn: SweepFn | None = None,
) -> dict[str, int]:
    """Run the dead-instance sweep, folding every failure to ``{}``.

    ``dry_run=True`` skips the sweep entirely — planning a start must
    not mutate the registry, not even tidily.
    """
    if dry_run:
        return {}
    try:
        result = (sweep_fn or _real_sweep)()
    except Exception:
        return {}
    if not isinstance(result, dict):
        return {}
    counters: dict[str, int] = {}
    for key, value in result.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value:
            counters[str(key)] = value
    return counters
