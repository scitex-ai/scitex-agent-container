"""The evidence vocabulary: the three states, the reporters, and the SENSORS.

Split out of :mod:`._verdict` (512-line cap) along the natural dependency edge:
this module is WHAT AN OBSERVATION IS, :mod:`._verdict` is WHAT OBSERVATIONS
MEAN. :class:`Signal` validates itself against the taxonomy declared here, so
the taxonomy has to be the lower layer.

A SOURCE IS A REPORTER. AN INSTRUMENT IS A SENSOR.
-------------------------------------------------
Conflating those two shipped a destruction gate that a single sensor could
satisfy by being asked twice.

``may_destroy`` demanded "2 independent sources" and counted SOURCE STRINGS. But
``process`` and ``registry`` are not two sensors — for BOTH runtimes they are the
SAME ``os.kill(pid, 0)`` on the SAME pid, and the codebase says so itself:

  ``runtimes/_tui_liveness.pane_pid_of``
    *"This is the value ``instances.pid`` records for a TUI agent, and it is the
    SAME signal ``pane_process_alive`` (hence ``is_running``) already keys
    liveness on — so the registry and ``is_running`` can never disagree about
    which process represents this agent."*

  ``runtimes/_apptainer_runtime.agent_pid``
    *"This is EXACTLY the pid ``is_running`` above probes with
    ``os.kill(pid, 0)`` ... reusing ``_read_pid`` here means the registry and
    ``is_running`` can never disagree."*

The two witnesses the gate demanded were ENGINEERED to agree. Corroboration from
one sensor asked twice is not corroboration — it is one observation wearing two
hats. That is precisely the failure that already burned this fleet: three agents
"independently corroborated" that the fleet was deaf, all by reading one
confounded ``inbox_subscribers == 0``.

So a :class:`Signal` must declare WHAT PHYSICALLY OBSERVED IT, and the gate
deduplicates by that BEFORE counting.

THE AMBIGUITY RULE (load-bearing)
---------------------------------
When a signal's instrument cannot be pinned down, label it with the instrument
that COLLAPSES (shares identity with an existing signal), never the one that ADDS
an independent witness. Under-counting witnesses refuses a destruction;
over-counting performs one. Only the second is unrecoverable.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ALIVE",
    "DEAD",
    "UNKNOWN",
    "SOURCE_DELIVERY",
    "SOURCE_HEARTBEAT",
    "SOURCE_PROCESS",
    "SOURCE_REGISTRY",
    "SOURCE_RESOLVER",
    "INSTRUMENT_AGENT_SELF",
    "INSTRUMENT_HOST_TMUX",
    "INSTRUMENT_LISTEN_BROKER",
    "INSTRUMENT_NO_OBSERVATION",
    "INSTRUMENT_PID_NAMESPACE",
    "INSTRUMENTS",
    "CONVICTING_INSTRUMENTS",
    "INSTRUMENT_INDEPENDENCE",
    "InstrumentSpec",
    "Signal",
]

# The three states. There is no fourth, and there is deliberately no bool.
ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"

# Signal SOURCES — the reporter that carried the news. Descending evidential
# strength. NOTE: a source is NOT a sensor; see the module docstring.
SOURCE_DELIVERY = "delivery"  # the broker OBSERVED the agent's inbox. Authoritative.
SOURCE_PROCESS = "process"  # a process/session probe (pane pid, apptainer pid).
SOURCE_HEARTBEAT = "heartbeat"  # a beat file someone refreshed.
SOURCE_REGISTRY = "registry"  # a row in a table. A declaration, not an observation.
SOURCE_RESOLVER = "resolver"  # the gatherer itself failed. Observes nothing, ever.

# Signal INSTRUMENTS — what PHYSICALLY made the observation.
INSTRUMENT_LISTEN_BROKER = "listen_broker"
INSTRUMENT_HOST_TMUX = "host_tmux"
INSTRUMENT_PID_NAMESPACE = "pid_namespace"
INSTRUMENT_AGENT_SELF = "agent_self"
INSTRUMENT_NO_OBSERVATION = "no_observation"


@dataclass(frozen=True)
class InstrumentSpec:
    """What one sensor physically reads, and what it is permitted to conclude.

    ``verdicts`` is the CLOSED set of verdicts this instrument may emit, and it
    is ENFORCED at :class:`Signal` construction. That turns two doctrines which
    were previously only prose into mechanism:

    * a delivery observation may never yield :data:`DEAD` (zero subscribers is a
      DETACHED adapter, not a corpse — the exact inference that convicted a live
      fleet);
    * a heartbeat may never yield :data:`DEAD` (its writer is shared, so a stale
      beat looks identical whether the AGENT died or the WRITER did).

    Write ``Signal(SOURCE_DELIVERY, DEAD, ...)`` today and it raises, instead of
    silently arming the destruction gate.
    """

    reads: str
    verdicts: frozenset[str]
    blind_when: str

    @property
    def may_convict(self) -> bool:
        """May a :data:`DEAD` from this instrument count toward destruction?"""
        return DEAD in self.verdicts


# EVERY instrument MUST have an entry here. This is not documentation: the suite
# FAILS if an instrument is added without being classified, and
# :meth:`Signal.__post_init__` refuses any instrument that is not a key. An
# enumeration nobody is forced to update is a promise of completeness that
# quietly stops being true.
INSTRUMENT_INDEPENDENCE: dict[str, InstrumentSpec] = {
    INSTRUMENT_LISTEN_BROKER: InstrumentSpec(
        reads=(
            "the sac listen broker's inbox-subscriber table — whether a message "
            "would actually WAKE this agent. The one signal that asks the agent "
            "instead of inspecting its shadow."
        ),
        # Never DEAD: zero subscribers is a DETACHED adapter, and agents with a
        # detached adapter have been measured answering peer messages the same
        # minute. UNREACHABLE must never be wired to anything destructive.
        verdicts=frozenset({ALIVE, UNKNOWN}),
        blind_when=(
            "there is no local listen to ask, or the agent lives on another host"
        ),
    ),
    INSTRUMENT_HOST_TMUX: InstrumentSpec(
        reads=(
            "the host tmux server's own session bookkeeping "
            "(``tmux list-sessions``) — whether a ``tui-<name>`` session exists."
        ),
        verdicts=frozenset({ALIVE, DEAD, UNKNOWN}),
        blind_when=(
            "tmux is wedged, or we are inside a container and the host's tmux "
            "socket is in another mount namespace (an EMPTY snapshot from in "
            "there is namespace blindness, not an empty fleet)"
        ),
    ),
    INSTRUMENT_PID_NAMESPACE: InstrumentSpec(
        reads=(
            "``os.kill(pid, 0)`` in THIS process's pid namespace — whether the "
            "recorded pid is reaped. Both ``runtime.is_running`` (for pid-based "
            "runtimes) and the ``instances`` row's pid check bottom out HERE, on "
            "the SAME pid, by explicit design. They are ONE instrument."
        ),
        verdicts=frozenset({ALIVE, DEAD, UNKNOWN}),
        blind_when=(
            "we are inside a container (a host pid is not in our pid namespace, "
            "so os.kill answers about a DIFFERENT process, or none at all), or "
            "the row belongs to another host. A pid read across a namespace "
            "boundary is not a weak sensor — it is NOT A SENSOR."
        ),
    ),
    INSTRUMENT_AGENT_SELF: InstrumentSpec(
        reads=(
            "a file the agent's OWN in-process loop refreshed (the SDK heartbeat "
            "writer). A fresh beat is the agent saying 'I am here'."
        ),
        # Never DEAD: a stale beat has two indistinguishable causes — the agent
        # went away, or its writer did. It convicts nobody.
        verdicts=frozenset({ALIVE, UNKNOWN}),
        blind_when="the beat file is unreadable from here (e.g. a container $HOME)",
    ),
    INSTRUMENT_NO_OBSERVATION: InstrumentSpec(
        reads=(
            "nothing at all. The gatherer itself failed, so no sensor ever ran. "
            "Exists so that 'we did not look' is a first-class, TYPED answer "
            "rather than an absence that a counter can mistake for evidence."
        ),
        verdicts=frozenset({UNKNOWN}),
        blind_when="always — by definition it observed nothing",
    ),
}

INSTRUMENTS = frozenset(INSTRUMENT_INDEPENDENCE)

# The instruments whose DEAD may count toward a destruction. Exactly two, and
# they are genuinely independent: NO SINGLE SENSOR MALFUNCTION CAN PRODUCE BOTH.
# A wedged or namespace-blind tmux yields UNKNOWN (not DEAD); a cross-namespace
# pid read yields UNKNOWN (not DEAD). So a corroborated DEAD means: the tmux
# server positively has no session for this agent, AND the kernel positively
# says the recorded pid is reaped. Two different bookkeepers, two different
# failure modes.
CONVICTING_INSTRUMENTS = frozenset(
    name for name, spec in INSTRUMENT_INDEPENDENCE.items() if spec.may_convict
)


@dataclass(frozen=True)
class Signal:
    """One observation about one agent.

    ``verdict`` is ternary — :data:`ALIVE`, :data:`DEAD` or :data:`UNKNOWN`.
    A probe that could not run MUST report :data:`UNKNOWN` with the reason in
    ``detail``; it must never report :data:`DEAD`, because "I did not see it"
    and "I saw that it is not there" are different facts and only the second
    one may be acted on.

    ``detail`` is operator-facing and is expected to be specific: not
    "unhealthy" but ``pid=0 in heartbeat.json — no verdict possible`` or
    ``no tmux session 'tui-grant' (probe succeeded)``. A row that reads
    ``running | pid=None`` teaches an operator nothing; that is the UX bug
    this field exists to fix.

    ``instrument`` is WHAT PHYSICALLY OBSERVED THIS — the sensor, as opposed to
    ``source``, which is merely the reporter that carried the news. It is
    REQUIRED, and it must be one of the classified :data:`INSTRUMENTS`, because
    the destruction gate counts DISTINCT INSTRUMENTS and a free string would let
    one sensor masquerade as two witnesses. That is not hypothetical — it is the
    bug this field was added to kill.
    """

    source: str
    verdict: str
    detail: str
    instrument: str

    def __post_init__(self) -> None:
        if self.verdict not in (ALIVE, DEAD, UNKNOWN):
            raise ValueError(
                f"Signal.verdict must be one of {ALIVE!r} / {DEAD!r} / "
                f"{UNKNOWN!r}, got {self.verdict!r}. A liveness signal is "
                f"never a bool — 'I could not tell' is a first-class answer "
                f"and must be spelled {UNKNOWN!r}, never collapsed into a pole."
            )
        spec = INSTRUMENT_INDEPENDENCE.get(self.instrument)
        if spec is None:
            raise ValueError(
                f"Signal.instrument must be one of {sorted(INSTRUMENTS)!r}, got "
                f"{self.instrument!r}. An instrument is the SENSOR that made the "
                f"observation, and the destruction gate counts DISTINCT "
                f"instruments — so an unclassified one could pose as an "
                f"independent second witness and authorise killing a healthy "
                f"agent. Declare it in INSTRUMENT_INDEPENDENCE (say what it "
                f"physically reads, which verdicts it may emit, and when it is "
                f"blind) rather than inventing a string here."
            )
        if self.verdict not in spec.verdicts:
            raise ValueError(
                f"instrument {self.instrument!r} may not emit {self.verdict!r} — "
                f"it is declared to emit only {sorted(spec.verdicts)!r}. It "
                f"reads: {spec.reads} If you believe it can now observe "
                f"{self.verdict!r}, change its InstrumentSpec and defend it "
                f"there; do not smuggle the claim in at a call site. (A DEAD "
                f"from an instrument that cannot see death is how a detached "
                f"inbox adapter got read as a corpse.)"
            )

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "verdict": self.verdict,
            "detail": self.detail,
            "instrument": self.instrument,
        }
