"""N corpses at once is ONE event, and it is not N agents dying.

THE HOLE THIS CLOSES
--------------------
``_runners/_tmux/_tmux_probe.py`` treats tmux's two "there is no server"
stderrs as a **confirmed-empty fleet** — it returns ``{}``, a real
observation, not ``None``. That is deliberate and right: a host with no tmux
server genuinely has no sessions.

``_lifecycle/_verdict_tmux._observed_snapshot`` then rescues the one case
where an empty reading is a lie — ``if not snapshot and in_sif_fn()`` — because
from inside a container the host's tmux lives in another mount namespace. But
this job runs under ``systemd --user`` **on the host**, where ``in_sif()`` is
``False``. So ``{}`` passes straight through as "every session is genuinely
absent".

Correct when a tmux server is normally absent. **Catastrophically wrong when
the server dying IS the failure mode** — and that is exactly what happened on
2026-08-11: the host's tmux server went away and took every pane with it,
eleven agents in a two-second window. Every one of them would read here as an
independent corpse with a spec asking to be kept running.

WHY THE PER-AGENT BUDGET DOES NOT COVER THIS
--------------------------------------------
:mod:`._budget` is real and it is not the thing that fails. It throttles **per
agent** (30-minute debounce, 2/agent/hour) and **per pass** (10). Neither is a
FLEET-wide limit, and the distinction only matters in this one scenario:

    10 restarts/pass x 12 passes/hour = up to 120 container starts per hour

on a host that has just had whatever event killed its tmux server. Ninety specs
on this fleet declare a managed restart policy, so the population is there. And
it does not even fail fast: ``tmux new-session`` SPAWNS a server, so
``sac agents start`` SUCCEEDS into a host that just lost one. The restarts land,
and keep landing.

THE RULE
--------
If a pass would restart more than one agent **and there is no tmux server on
this host at all**, those restarts share a cause that is not the agents, and
the reconciler must refuse and say so.

The predicate is SERVER-ABSENT, not zero-sessions, and the difference is the
whole design. Both incidents below present an empty session list, and they
demand opposite responses:

* **server alive, every agent dead** — the 2026-06 OAuth rotation killed 33
  agents while tmux was untouched. RECOVER THEM. That incident is why this job
  exists, and ``test_whole_dead_fleet_is_recovered`` is it written down.
* **server itself gone** — 2026-08-11. REFUSE.

An earlier draft of this module keyed on "zero live sessions" and would have
blocked the first case. The suite caught it on the first run. The fact that
separates them was already in the probe's hands and was being dropped at the
boundary; :func:`.._runners._tmux._tmux_probe.list_sessions_activity_detailed`
now carries it.

Two more things this deliberately does NOT do:

* A pass that would restart nothing is unaffected — an idle host reports no
  sessions too, and there is nothing to refuse.
* A SINGLE corpse is still restarted, even with the server gone. Refusing there
  would strand a one-agent host, and one restart is a blast radius the
  per-agent budget already bounds.

So the threshold is where the evidence changes: **two or more corpses and no
tmux server at all** is a statement about the host, not about the agents.

This never restarts anything, and it never marks an agent dead. It withholds
an action and raises an alarm — the strictly safer direction, because the cost
of refusing wrongly is a delayed restart the next pass can still perform, while
the cost of acting wrongly is a fleet-wide restart storm into an unhealthy
host.
"""

from __future__ import annotations

__all__ = [
    "FLEET_BLACKOUT_MIN_RESTARTS",
    "blackout_detail",
    "is_fleet_blackout",
]

#: How many would-be restarts it takes, with zero sessions observed, before a
#: pass stops believing it is looking at independent deaths. Two: one corpse on
#: a host with no other sessions is ordinary (and bounded by the per-agent
#: budget), while two simultaneous corpses and no survivors anywhere is the
#: signature of something that killed them together.
FLEET_BLACKOUT_MIN_RESTARTS = 2


def is_fleet_blackout(
    *,
    server_present: bool | None,
    restart_count: int,
    min_restarts: int = FLEET_BLACKOUT_MIN_RESTARTS,
) -> bool:
    """Is this pass looking at ONE infrastructure event rather than N deaths?

    ``server_present`` comes from
    :func:`.._runners._tmux._tmux_probe.list_sessions_activity_detailed`:
    ``True`` a tmux server answered, ``False`` there is none, ``None`` we could
    not tell.

    ONLY ``False`` TRIPS THIS, and that is the whole correction. An earlier
    draft keyed on "zero live sessions", which is the same reading for two
    opposite incidents:

        server alive, every agent dead   the 2026-06 OAuth rotation — 33 agents
                                         killed, tmux untouched. RECOVER THEM;
                                         this job exists for that case.
        server itself gone               2026-08-11 — eleven agents in two
                                         seconds. REFUSE; restarting here
                                         lands on a host that just lost its
                                         tmux server and repeats every pass.

    Keying on the empty session list would have blocked the first, which the
    suite caught immediately: ``test_whole_dead_fleet_is_recovered`` is that
    incident written down. The distinguishing fact was already in the probe's
    hands (``rc != 0`` + a no-server marker, versus ``rc == 0`` and no rows)
    and was simply being discarded at the boundary.

    ``None`` never trips it: an unobservable fleet is already handled upstream
    (every agent decides ``UNKNOWN`` and nothing is restarted), and inventing
    a blackout from a reading nobody took would be the same manufactured
    certainty one layer along.

    ``restart_count`` is how many agents this pass decided to RESTART, counted
    BEFORE any of them is performed — which is why the caller must decide for
    the whole fleet first and act second.
    """
    if server_present is not False:
        return False
    return restart_count >= min_restarts


def blackout_detail(restart_count: int, names: tuple[str, ...] = ()) -> str:
    """The operator-facing explanation. Says what was seen, what was withheld,
    and what to check — a refusal whose reason the reader has to guess is how a
    correct decision still ends in the wrong action.
    """
    listed = ", ".join(names[:10]) if names else "(none named)"
    more = f" (+{len(names) - 10} more)" if len(names) > 10 else ""
    return (
        f"FLEET BLACKOUT — {restart_count} agent(s) look like corpses and "
        f"THERE IS NO TMUX SERVER on this host at all. The server itself went "
        f"away and took every pane with it (as on 2026-08-11, eleven agents "
        f"in two seconds) — this is ONE event, not {restart_count} agents "
        f"dying independently. REFUSING to restart anything: a mass restart "
        f"here would LAND (tmux new-session spawns a fresh server), so it "
        f"would not even fail loudly, and it would repeat every pass. "
        f"Withheld: {listed}{more}. Find why the tmux server went away, then "
        f"run `sac agents reconcile --apply --limit 2` by hand and watch it. "
        f"NOTE this is specifically a MISSING SERVER — a live server holding "
        f"zero sessions means the agents died and DOES restart them."
    )
