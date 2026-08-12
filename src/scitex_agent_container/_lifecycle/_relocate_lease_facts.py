"""Read the lease row, and go and LOOK at whatever host it names.

The adapter behind :func:`._relocate_checks_late.check_lease_holdable`. The check
is a pure predicate over facts; this is the part that opens the state db and, when
the answer depends on it, spends one ssh asking a third machine whether it is
running the agent.

TWO SOURCES, AND THEY ARE NOT INTERCHANGEABLE. The row comes from the
COORDINATOR's own store — which is the whole of the problem it exists to describe,
since the coordinator is always the host being LEFT and the destination of a move
never learns what happened. The liveness comes from the host the row NAMES, over
the same tmux question the runtime asks. One without the other is exactly the
material the 2026-08-11 canary refused on: a true row, read as a live writer,
about a machine nobody had asked.

WHAT ``read=False`` PROTECTS. An unreadable or absent store must leave ``read``
false, NOT ``lease=None`` — because ``lease=None`` is a real and common answer
("this store has never held a row for this agent") that BOOTSTRAPS a lease and
proceeds. Collapsing "I could not open the db" into it would turn a broken store
into a green light, at the one gate that stands between one live agent and two.

THE OBSERVATION IS ONLY TAKEN WHEN IT CAN CHANGE THE ANSWER — no row, a row
naming the source itself, or an expired row all decide without it. Probing anyway
would let an idle machine's ssh timeout refuse a relocation that was fine, which
is a new way to fail rather than a safer one.
"""

from __future__ import annotations

import time
from typing import Callable

from ._relocate_preflight_facts import LeaseFacts

__all__ = ["gather_lease_facts"]


def gather_lease_facts(
    agent: str,
    *,
    from_host: str,
    local_host: str = "",
    now: float | None = None,
    exec_fn: Callable[..., dict] | None = None,
    db_path=None,
    load: Callable[[str], object] | None = None,
    observe: Callable[[str, str], tuple[bool | None, str]] | None = None,
) -> LeaseFacts:
    """Read the agent's lease and, if it names a live third host, observe that host.

    ``load`` and ``observe`` are the injection seams, and they take and return
    real values rather than standing in for the behaviour under test: a test
    passes a callable that returns a real :class:`._relocate_lease.Lease` and a
    real ``(bool | None, str)``, so nothing about the decision is mocked.

    Never raises. Every failure becomes ``read=False`` or an unobserved liveness,
    both of which the check reports as UNKNOWN and refuses on.
    """
    moment = time.time() if now is None else float(now)
    store = _store_path(db_path)

    reader = load if load is not None else _default_load(db_path)
    try:
        lease = reader(agent)
    except Exception as exc:  # stx-allow: fallback (reason: an unreadable lease store must leave the fact UNOBSERVED, never "no row", which would bootstrap a lease and proceed)
        return LeaseFacts(
            read=False,
            store=store,
            now=moment,
            recorded_holder_evidence=(
                f"the lease store could not be read: {type(exc).__name__}: {exc}"
            ),
        )

    holder = getattr(lease, "holder", "") if lease is not None else ""
    if not _needs_observing(lease, from_host, moment):
        return LeaseFacts(read=True, lease=lease, store=store, now=moment)

    watcher = observe if observe is not None else _default_observe(local_host, exec_fn)
    try:
        running, why = watcher(holder, agent)
    except Exception as exc:  # stx-allow: fallback (reason: a failed probe is NOT MEASURED; folding it into "not running" would hand the lease away from a possibly-live writer)
        running, why = None, f"the liveness probe of {holder} raised {type(exc).__name__}: {exc}"
    return LeaseFacts(
        read=True,
        lease=lease,
        recorded_holder_running=running,
        recorded_holder_evidence=why,
        store=store,
        now=moment,
    )


def _needs_observing(lease, from_host: str, now: float) -> bool:
    """Only a LIVE row naming somebody other than the source needs a third host asked.

    An unnamed source is deliberately in the "no" set: the check refuses on that
    alone, so an ssh spent here would buy nothing and could only add a way to
    fail.
    """
    if lease is None or not from_host:
        return False
    if getattr(lease, "holder", "") == from_host:
        return False
    expired = getattr(lease, "is_expired", None)
    return not (callable(expired) and expired(now))


def _store_path(db_path) -> str:
    """Which db was read. Reported because a lease answer is only as good as its store."""
    if db_path is not None:
        return str(db_path)
    try:
        from .._state.state_db import DEFAULT_DB_PATH

        return str(DEFAULT_DB_PATH)
    except Exception:  # stx-allow: fallback (reason: the store's NAME is for the report; failing to render it must not cost the reader the lease answer itself)
        return ""


def _default_load(db_path) -> Callable[[str], object]:
    def load(agent: str):
        from .._state.state_db_relocation import load_lease

        return load_lease(agent, db_path=db_path)

    return load


def _default_observe(
    local_host: str, exec_fn: Callable[..., dict] | None
) -> Callable[[str, str], tuple[bool | None, str]]:
    def observe(holder: str, agent: str) -> tuple[bool | None, str]:
        from ._relocate_liveness import observe_running
        from ._relocate_shell import shell_for

        shell = shell_for(holder, local_host=local_host or None)
        return observe_running(shell, agent, exec_fn=exec_fn)

    return observe
