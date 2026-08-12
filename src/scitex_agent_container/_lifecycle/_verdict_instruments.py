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
    "WEDGED",
    "SOURCE_DELIVERY",
    "SOURCE_HEARTBEAT",
    "SOURCE_PROCESS",
    "SOURCE_REGISTRY",
    "SOURCE_RESOLVER",
    "SOURCE_SCREEN",
    "INSTRUMENT_AGENT_SELF",
    "INSTRUMENT_HOST_TMUX",
    "INSTRUMENT_LISTEN_BROKER",
    "INSTRUMENT_NO_OBSERVATION",
    "INSTRUMENT_PID_NAMESPACE",
    "INSTRUMENT_TUI_SCREEN",
    "INSTRUMENTS",
    "CONVICTING_INSTRUMENTS",
    "INSTRUMENT_INDEPENDENCE",
    "InstrumentSpec",
    "Signal",
]

# The three CORE states — ALIVE / DEAD / UNKNOWN — and WEDGED, a fourth that is
# deliberately NOT a pole. There is still no bool.
#
# ALIVE / DEAD / UNKNOWN are the ternary every PROXY sensor speaks: observed
# alive, a corroborated absence, or "I could not tell". WEDGED is different in
# KIND — it is the answer of the one instrument that reads whether the agent is
# WORKING rather than merely PRESENT. The process exists and its tmux session is
# up (so every pid/session proxy reads ALIVE), yet a frozen auth-rejection banner
# sits on its pane and it is doing nothing. WEDGED is NOT a pole:
#
#   * a wedged agent is PRESENT, so DEAD — "positive evidence of absence" — would
#     be a lie, and destruction is NEVER authorised on it (``may_destroy`` gates
#     on DEAD, so a WEDGED verdict can arm nothing); yet
#   * it is not WORKING, so ALIVE would be exactly the false-green this state
#     exists to kill (``is_alive`` stays ``verdict == ALIVE``, so WEDGED reads
#     is_alive False by construction — the hard requirement).
#
# See :func:`.._verdict.decide` for where WEDGED sits in the precedence (below a
# delivery-ALIVE, above the pid/session positive-life step).
ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"
WEDGED = "wedged"

# Signal SOURCES — the reporter that carried the news. Descending evidential
# strength. NOTE: a source is NOT a sensor; see the module docstring.
SOURCE_DELIVERY = "delivery"  # the broker OBSERVED the agent's inbox. Authoritative.
SOURCE_PROCESS = "process"  # a process/session probe (pane pid, apptainer pid).
SOURCE_HEARTBEAT = "heartbeat"  # a beat file someone refreshed.
SOURCE_REGISTRY = "registry"  # a row in a table. A declaration, not an observation.
SOURCE_RESOLVER = "resolver"  # the gatherer itself failed. Observes nothing, ever.
SOURCE_SCREEN = (
    "screen"  # the rendered CONTENT of the tui pane. Reads WORKING, not presence.
)
# the agent's OWN turn record — the only artefact written BY the failure itself.
SOURCE_TRANSCRIPT = "transcript"

# Signal INSTRUMENTS — what PHYSICALLY made the observation.
INSTRUMENT_LISTEN_BROKER = "listen_broker"
INSTRUMENT_HOST_TMUX = "host_tmux"
INSTRUMENT_PID_NAMESPACE = "pid_namespace"
INSTRUMENT_AGENT_SELF = "agent_self"
INSTRUMENT_NO_OBSERVATION = "no_observation"
INSTRUMENT_TUI_SCREEN = "tui_screen"
INSTRUMENT_TURN_REFUSAL = "turn_refusal"


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
    INSTRUMENT_TUI_SCREEN: InstrumentSpec(
        reads=(
            "the rendered CONTENT of the tui-<name> pane — whether a FROZEN auth "
            "banner sits directly above the input prompt (read from the cached "
            "``sac agents auth-status`` verdict, itself produced by the "
            "2-run-frozen matcher). It observes whether the agent is WORKING, as "
            "opposed to every pid/session sensor which observes only that the "
            "process is PRESENT. A tmux-GREEN agent stuck under an auth-rejection "
            "banner — pid alive, session up, doing nothing — is the exact "
            "false-green this instrument exists to catch."
        ),
        # Never ALIVE (a clean pane is not proof of life — the agent may be busy,
        # idle, or wedged in a way this banner match does not recognise) and
        # never DEAD (the pane, and the process behind it, are PRESENT — a wedged
        # agent is not a corpse, and DEAD would arm a destruction against a living
        # process). It emits exactly WEDGED (a frozen banner) or UNKNOWN. Because
        # DEAD ∉ verdicts, ``may_convict`` is False and it is absent from
        # CONVICTING_INSTRUMENTS — it can NEVER arm a destroy.
        verdicts=frozenset({WEDGED, UNKNOWN}),
        blind_when=(
            "the pane is uncapturable, there is no tui-<name> session on this "
            "host, we are inside a container (the host's tmux is in another mount "
            "namespace), or the auth-status cache is stale or absent — all of "
            "which degrade to UNKNOWN, never a false WEDGE"
        ),
    ),
    INSTRUMENT_TURN_REFUSAL: InstrumentSpec(
        reads=(
            "the LAST assistant turn in the agent's own Claude Code transcript — "
            "whether the provider REFUSED it. Claude Code stamps a refused turn "
            "``isApiErrorMessage: true`` with ``model: '<synthetic>'`` and zero "
            "token usage: no turn ran. That record is written BY the failure, by "
            "the agent, at the moment it could not act — so unlike every other "
            "sensor here it is not a proxy for the fault, it IS the fault's own "
            "receipt. It answers 'can this agent execute a turn?', which no "
            "pid, session, port or heartbeat sensor asks.\n"
            "Measured 2026-08-10 on scitex-compute-04, the three refusals this "
            "reads, all captured verbatim in the fixture: \"You've hit your "
            "weekly limit · resets 11pm (UTC)\" (quota), \"Not logged in · "
            "Please run /login\" and a 401 \"OAuth access token has expired\" "
            "(credentials). Every sac surface called that agent HEALTHY "
            "throughout, and the operator diagnosed it by reading the pane."
        ),
        # Never ALIVE: a last turn that SUCCEEDED proves the agent could act
        # THEN, not that it can act now — the same restraint the screen
        # instrument practises with a clean pane. Never DEAD: an agent refusing
        # turns is emphatically PRESENT, and DEAD would arm a destruction
        # against a living process whose only fault is an exhausted quota (which
        # a restart does not fix — it would destroy context for nothing). It
        # emits exactly WEDGED (present but unable to act) or UNKNOWN. Because
        # DEAD is absent, ``may_convict`` is False and it can never arm a
        # destroy.
        verdicts=frozenset({WEDGED, UNKNOWN}),
        blind_when=(
            "no transcript can be located for this agent (its container $HOME is "
            "not on this host, the spec-derived candidate homes are a PROMISE "
            "about where a transcript would go rather than a fact about where "
            "this incarnation wrote one, or the agent has not written a turn "
            "yet), the file is unreadable, or its last turn is older than the "
            "freshness window — all of which degrade to UNKNOWN, never to a "
            "false 'able to act'"
        ),
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
        if self.verdict not in (ALIVE, DEAD, UNKNOWN, WEDGED):
            raise ValueError(
                f"Signal.verdict must be one of {ALIVE!r} / {DEAD!r} / "
                f"{UNKNOWN!r} / {WEDGED!r}, got {self.verdict!r}. A liveness "
                f"signal is never a bool — 'I could not tell' is a first-class "
                f"answer and must be spelled {UNKNOWN!r}, never collapsed into a "
                f"pole. {WEDGED!r} (present but not working) is the one non-pole "
                f"state, and the InstrumentSpec still decides WHICH instrument "
                f"may emit it (only the screen sensor), so a "
                f"Signal(..., {WEDGED!r}, ...) on any other instrument still "
                f"raises below."
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


def delivery_alive_wins(signals: "tuple[Signal, ...]") -> bool:
    """Does a ``delivery`` ALIVE settle this fold outright? (rule 1 of decide)

    A delivery ALIVE is normally supreme — above even WEDGED — because the
    broker OBSERVED the agent's inbox adapter attached, and a broker-reachable
    agent was taken to be demonstrably working. That premise held for the
    founding auth-death incident, whose agent was NOT reachable.

    IT DOES NOT HOLD FOR A REFUSED TURN, and the 2026-08-10 ``scitex-cards``
    incident is the counter-example: the operator's messages REACHED that agent
    — that is how they got refused, one "You've hit your weekly limit" per
    message — for 32 minutes, while every surface reported HEALTHY.

    Delivery observes that a message can ARRIVE. It does not observe that a turn
    can EXECUTE. So when :data:`INSTRUMENT_TURN_REFUSAL` reports WEDGED — the
    agent's OWN transcript recording that its most recent turn did not run — the
    delivery shortcut is withdrawn and the fold falls through to the WEDGED
    rule. Deliberately narrow: ONLY that instrument suppresses it, because only
    that one reads the agent's testimony about its own turns; a screen WEDGE
    (a rendered banner, which may be history) still loses to delivery exactly as
    before.
    """
    for sig in signals:
        if sig.verdict == WEDGED and sig.instrument == INSTRUMENT_TURN_REFUSAL:
            return False
    return any(
        sig.verdict == ALIVE and sig.source == SOURCE_DELIVERY for sig in signals
    )
