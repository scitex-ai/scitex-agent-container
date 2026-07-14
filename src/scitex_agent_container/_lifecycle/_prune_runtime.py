"""Prune per-agent runtime + overlay dirs for ephemeral agents on stop.

Inode-hygiene fix for the sac-runtime-state-hygiene incident: ephemeral
capsule agents accumulate one runtime dir (``<runtime-base>/<name>/``:
boot logs, heartbeat.json, session.jsonl, home/, tmp scratch) plus one
overlay upper layer per run. Across a large launch wave that exhausts a
shared GPFS fileset's inode budget (the incident: 1.25M+ inodes from a
cohort's per-capsule capsules).

On a CLEAN terminal stop we prune BOTH for agents that OPT IN.

Gate (conservative — :func:`should_prune_runtime`)::

    config.restart.policy == "never"  AND  config.restart.prune_on_stop

The explicit ``prune_on_stop`` opt-in is REQUIRED (not defaulted-on for
never-policy) because ``restart.policy`` DEFAULTS to ``"never"`` in the
config model: a persistent coordinator that simply omits a ``restart:``
block is policy==never yet MUST keep its runtime (session_id, home/)
across restarts. Requiring the flag guarantees persistent agents are
NEVER pruned.

The prune runs only from the genuine terminal-stop entry point
(``sac agents stop``, which passes ``prune_runtime=True`` to
``agent_stop``). The internal ``agent_stop`` calls made by
``agent_restart`` / force-``agent_start`` keep the default
``prune_runtime=False``, so a restart never nukes the runtime it is
about to reuse.

Best-effort + fail-LOUD-log: every path removed / skipped / failed is
logged; a removal error is logged (never raised) so teardown always
completes.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import AgentConfig

logger = logging.getLogger(__name__)


def should_prune_runtime(config: "AgentConfig") -> bool:
    """Return True iff ``config`` is an opted-in ephemeral agent.

    Both conditions are required:
      * ``restart.policy == "never"`` (semantically never comes back), AND
      * ``restart.prune_on_stop is True`` (explicit opt-in).
    """
    restart = getattr(config, "restart", None)
    if restart is None:
        return False
    return (
        getattr(restart, "policy", "never") == "never"
        and bool(getattr(restart, "prune_on_stop", False))
    )


def _rmtree_loud(path: Path | None, label: str, name: str) -> str | None:
    """Best-effort ``rmtree`` with loud logging. Returns the removed path.

    Returns the string path when something was removed, else ``None``
    (absent, unresolved, or a removal error — all logged).
    """
    if path is None:
        logger.info("prune[%s]: %s unresolved, nothing to prune", name, label)
        return None
    try:
        if not path.exists():
            logger.info(
                "prune[%s]: %s %s absent, nothing to prune", name, label, path
            )
            return None
        shutil.rmtree(path)
    except OSError as exc:  # stx-allow: fallback (reason: prune is best-effort teardown hygiene; a removal failure must be logged, never raised, so the stop path always completes)
        logger.warning(
            "prune[%s]: FAILED to remove %s %s: %s", name, label, path, exc
        )
        return None
    logger.warning("prune[%s]: removed %s %s", name, label, path)
    return str(path)


def _resolve_runtime_dir(name: str) -> Path | None:
    """Resolve ``<runtime-base>/<name>`` (the per-agent state dir)."""
    try:
        from .._runners._session_state import state_dir_for

        return state_dir_for(name)
    except Exception:  # stx-allow: fallback (reason: partial install / import failure must not block the rest of the prune — the overlay leg still runs)
        logger.warning("prune[%s]: could not resolve runtime dir", name)
        return None


def _resolve_overlay_dir(config: "AgentConfig") -> Path | None:
    """Resolve the agent's directory-form apptainer overlay dir, or None.

    Reuses the runtime's own resolver so the pruned path is exactly the
    one apptainer writes into. ``.img`` overlays resolve to ``None``
    (loopback images are a single inode, not the accumulation problem).
    """
    try:
        from ..runtimes._to_home_overlay import _resolve_overlay_dir as _resolve

        return _resolve(config)
    except Exception:  # stx-allow: fallback (reason: overlay resolution is optional; a resolver import/failure must not block the runtime-dir prune)
        logger.warning("prune[%s]: could not resolve overlay dir", config.name)
        return None


def prune_agent_runtime(config: "AgentConfig") -> list[str]:
    """Prune the runtime dir + overlay for an opted-in ephemeral agent.

    Removes ONLY the bulk runtime + overlay (the inode accumulators) —
    never any output dir a launcher writes elsewhere. Best-effort; every
    action is logged. Returns the list of paths actually removed (for
    logging / tests).

    Callers gate on :func:`should_prune_runtime` first; this function
    re-checks nothing so a caller can also force a prune deliberately.
    """
    name = config.name
    removed: list[str] = []

    r = _rmtree_loud(_resolve_runtime_dir(name), "runtime dir", name)
    if r is not None:
        removed.append(r)

    r = _rmtree_loud(_resolve_overlay_dir(config), "overlay dir", name)
    if r is not None:
        removed.append(r)

    if not removed:
        logger.info("prune[%s]: nothing pruned (all targets absent)", name)
    return removed


def maybe_prune_agent_runtime(config: "AgentConfig") -> list[str]:
    """Prune iff :func:`should_prune_runtime`; log the skip reason otherwise.

    The single entry point the stop path calls. Never raises.
    """
    if should_prune_runtime(config):
        return prune_agent_runtime(config)
    restart = getattr(config, "restart", None)
    logger.info(
        "prune[%s]: skipped (policy=%s prune_on_stop=%s) — persistent/"
        "non-opted-in agents are never pruned",
        config.name,
        getattr(restart, "policy", "?"),
        getattr(restart, "prune_on_stop", "?"),
    )
    return []
