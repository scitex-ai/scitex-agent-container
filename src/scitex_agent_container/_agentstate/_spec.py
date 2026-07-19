"""THE SPEC: which signals exist, which are load-bearing, which are decisive.

状態は spec で決まっている. Adding a criterion is an edit HERE — one table — not
an edit at N call sites. That is the whole point: the mess was never the signals,
it was the combining logic hidden at every place that asked a question
(「内部でごちゃごちゃやるからおかしくなるじゃんね」). Each caller re-derived
"is this agent OK" from whatever subset it happened to hold, and two callers on
one host produced two different answers minutes apart.

WHAT EACH FLAG MEANS
--------------------
``healthy``
    The value of this predicate that means "fine". It is NOT always ``True``:
    ``is_login_required`` is healthy when ``False``. The predicates are named for
    what they literally assert, and the spec — not the reader — says which pole
    is the good one. Only ``load_bearing`` signals ever consult this.

``load_bearing``
    This signal participates in the verdict. Its unhealthy value REFUTES; its
    ``None`` renders the whole verdict UNKNOWN. Everything else is carried as
    evidence — rendered in every row, archived in the journal — but does not
    drive the answer.

    Load-bearingness is chosen by FALSE-RED COST, and every entry below states
    the measurement it rests on. The signals that are NOT load-bearing are not
    weak; they are KNOWN to read unhealthy on demonstrably healthy agents, so
    promoting them would manufacture the false RED this exercise exists to
    abolish. A verdict that flags a working fleet gets turned off, and then
    there is no verdict at all.

``decisive``
    A load-bearing signal whose UNHEALTHY value short-circuits to False even when
    other load-bearing signals are ``None``. Without it, one unreadable signal
    renders UNKNOWN and blocks repair of a genuinely dead agent — and something
    is always unreadable somewhere.

    DECISIVE REQUIRES DIRECT OBSERVATION, NEVER INFERENCE, and that is ENFORCED
    below rather than merely asked for: :func:`validate_specs` raises if a
    decisive signal is not :data:`OBSERVATION_DIRECT`. This is the existing "only
    a CORROBORATED negative may destroy" doctrine kept honest by mechanism — a
    signal read out of a cache or a declaration can never become decisive by
    someone flipping one flag.

``evidence``
    The RAW capture keys this signal was read out of. The reason the journal can
    answer "what did this agent's pane actually look like at 20:20?" long after
    the fact, instead of only "we concluded UNKNOWN".
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DECISIVE_SIGNALS",
    "LOAD_BEARING",
    "OBSERVATION_DIRECT",
    "OBSERVATION_INFERRED",
    "SIGNAL_NAMES",
    "SIGNALS",
    "SignalSpec",
    "spec_for",
    "validate_specs",
]

#: The sensor physically read the thing itself, in this process, just now — a
#: tmux server's own session list, the process table, the bytes on a pane.
OBSERVATION_DIRECT = "direct"

#: The value came from a cache, a declaration, a file another process wrote, or a
#: deduction. Perfectly good evidence; never grounds for a decisive verdict.
OBSERVATION_INFERRED = "inferred"


@dataclass(frozen=True)
class SignalSpec:
    """One criterion, and everything the aggregation needs to know about it."""

    name: str
    reads: str
    healthy: bool
    load_bearing: bool
    why: str
    decisive: bool = False
    observation: str = OBSERVATION_INFERRED
    evidence: tuple[str, ...] = ()


SIGNALS: tuple[SignalSpec, ...] = (
    SignalSpec(
        name="is_tmux_live",
        reads=(
            "the host tmux server's own session list — whether a tui-<agent> "
            "session exists on the server the live fleet actually runs on"
        ),
        healthy=True,
        load_bearing=True,
        decisive=False,
        observation=OBSERVATION_DIRECT,
        evidence=("tmux_sessions",),
        why=(
            "LOAD-BEARING: for a TUI agent the session IS the agent's existence on "
            "this host. NOT decisive, because an EMPTY session list is exactly "
            "what a container sees of the host's tmux (a different mount "
            "namespace) — measured 2026-07-14, a false DEAD against an agent "
            "holding a live session. A blind read must render None here, and a "
            "None must never be allowed to convict."
        ),
    ),
    SignalSpec(
        name="is_process_alive",
        reads=(
            "the process table for the pane's own pid, read directly in this "
            "process's pid namespace, with the raw ps line (including start time) "
            "kept verbatim"
        ),
        healthy=True,
        load_bearing=True,
        decisive=True,
        observation=OBSERVATION_DIRECT,
        evidence=("ps_line", "pane_pid"),
        why=(
            "LOAD-BEARING and DECISIVE. A directly observed absent process is the "
            "one negative this system may act on while other signals are "
            "unreadable: if every None blocked a verdict, a genuinely dead agent "
            "could never be repaired. Decisive ONLY because it is read from the "
            "process table directly — the same fact taken from a registry row or "
            "a cache is a declaration ABOUT a pid, not an observation OF one, and "
            "stays non-decisive. The namespace rule still binds ABOVE this flag: "
            "a pid read from inside a container is not a weak sensor, it is NOT A "
            "SENSOR, and the observer must render None rather than False."
        ),
    ),
    SignalSpec(
        name="is_login_required",
        reads=(
            "the rendered CONTENT of the tui-<agent> pane: whether a system auth "
            "banner sits directly above the input prompt AND stayed frozen across "
            "two captures taken --interval apart"
        ),
        healthy=False,
        load_bearing=True,
        decisive=False,
        observation=OBSERVATION_DIRECT,
        evidence=("pane_run1", "pane_run2"),
        why=(
            "LOAD-BEARING, healthy when FALSE. The signal the whole 2026-07-17/18 "
            "incident turns on: an agent wedged behind 'Login expired' holds a "
            "live session and a live pid, so every presence-shaped signal reads "
            "fine while it does nothing. NOT decisive: the matcher reads rendered "
            "text, and a banner that MOVED means the agent is working or merely "
            "quoting the incident. The two-capture corroboration is what makes a "
            "True trustworthy, so a single unreadable capture must render None, "
            "never a wedge — a false wedge bounces a working agent and destroys "
            "its context."
        ),
    ),
    SignalSpec(
        name="is_at_idle_prompt",
        reads="whether an input-prompt line was located on the captured pane",
        healthy=True,
        load_bearing=False,
        observation=OBSERVATION_DIRECT,
        evidence=("pane_run1", "pane_run2"),
        why=(
            "EVIDENCE ONLY. Sitting at an idle prompt is the normal, healthy state "
            "of an agent waiting for work, so it can never refute. It is carried "
            "because it is the PRECONDITION of the near-prompt matcher: when no "
            "prompt was found, is_login_required's reading is much weaker, and a "
            "reader deserves to be shown that rather than left to infer it."
        ),
    ),
    SignalSpec(
        name="is_session_advancing",
        reads=(
            "the agent's session transcript — whether it grew between two reads, "
            "recorded together with the PATH actually inspected"
        ),
        healthy=True,
        load_bearing=False,
        observation=OBSERVATION_DIRECT,
        evidence=("session_path", "session_bytes"),
        why=(
            "EVIDENCE ONLY. An idle agent is not advancing and is perfectly "
            "healthy, so this cannot refute. It is carried WITH THE PATH IT READ "
            "because of the false zero: the transcript path was derived from an "
            "ASSUMPTION about the agent's workdir, so a wrong path produced "
            "session_jsonl_bytes:0 and rendered a live agent dead. A path that "
            "does not exist MUST render None; only a path found and read may "
            "render a number."
        ),
    ),
    SignalSpec(
        name="is_auth_probe_ok",
        reads="the cached sac agents auth-status verdict for this agent",
        healthy=True,
        load_bearing=False,
        observation=OBSERVATION_INFERRED,
        evidence=("auth_cache",),
        why=(
            "EVIDENCE ONLY, and inferred: a value another pass wrote to a cache at "
            "some past moment, so it describes the fleet as it was. It is also why "
            "nothing here may probe auth live — a probe that mutates is not a "
            "probe; refreshing a credential to 'check' it revoked every "
            "co-tenant's in-memory token. Read the cache, never rotate."
        ),
    ),
    SignalSpec(
        name="is_inbox_reachable",
        reads="the sac listen broker's inbox-subscriber table for this agent",
        healthy=True,
        load_bearing=False,
        observation=OBSERVATION_INFERRED,
        evidence=("inbox_probe",),
        why=(
            "EVIDENCE ONLY. Zero subscribers means a DETACHED inbox adapter, not a "
            "corpse — measured: agents with 0 subscribers have answered peer "
            "messages the same minute. Load-bearing here would flag a working "
            "fleet. Its POSITIVE is strong (the broker watched a message be able "
            "to wake the agent); its negative convicts nobody."
        ),
    ),
    SignalSpec(
        name="is_heartbeat_fresh",
        reads="the mtime of this agent's heartbeat.json",
        healthy=True,
        load_bearing=False,
        observation=OBSERVATION_INFERRED,
        evidence=("heartbeat",),
        why=(
            "EVIDENCE ONLY. For a TUI agent the beat is written by a SHARED loop "
            "inside sac listen, so a stale beat looks identical whether the AGENT "
            "went away or the WRITER did — one abandoned loop freezes twenty "
            "agents' beats at once. A signal that fails for twenty agents because "
            "of one unrelated process must never refute."
        ),
    ),
    SignalSpec(
        name="is_registry_active",
        reads="whether the instances table declares this agent running",
        healthy=True,
        load_bearing=False,
        observation=OBSERVATION_INFERRED,
        evidence=("registry_row",),
        why=(
            "EVIDENCE ONLY — a registry row is a DECLARATION someone wrote once, "
            "not an observation. It vouches for corpses and is routinely absent "
            "for healthy agents. It is carried precisely BECAUSE it disagrees: on "
            "2026-07-18 `auth-status` enumerated an agent `sac agents list` "
            "omitted, and the registry said 'defined' while tmux held a live "
            "session and pid 2620416 was alive. Under one always-returned state "
            "that contradiction is a VISIBLE row (is_tmux_live=True, "
            "is_registry_active=False) instead of two tools disagreeing in the "
            "dark, each confident and neither aware of the other."
        ),
    ),
)


def validate_specs(specs: "tuple[SignalSpec, ...]" = SIGNALS) -> None:
    """Refuse a spec table that would let an INFERENCE convict.

    Called at import, so a bad table fails the process rather than shipping. All
    three invariants have a real failure behind them: a duplicate name lets one
    signal silently shadow another in the lookup; a decisive signal that is not
    load-bearing would short-circuit a verdict it takes no part in; and a
    decisive signal read from a cache or a declaration is exactly the "confident
    answer about a thing we never observed" this module exists to end.
    """
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValueError(
                f"duplicate signal {spec.name!r} in the spec table — one entry "
                f"would silently shadow the other, and half the callers would be "
                f"reading a criterion nobody knows is there"
            )
        seen.add(spec.name)
        if spec.decisive and not spec.load_bearing:
            raise ValueError(
                f"{spec.name!r} is decisive but not load-bearing — a signal that "
                f"does not participate in the verdict cannot short-circuit it"
            )
        if spec.decisive and spec.observation != OBSERVATION_DIRECT:
            raise ValueError(
                f"{spec.name!r} is decisive but its observation is "
                f"{spec.observation!r}. DECISIVE REQUIRES DIRECT OBSERVATION: a "
                f"decisive signal overrides every other signal's UNKNOWN, so it "
                f"must be a thing we read ourselves, just now. A cached verdict or "
                f"a registry declaration may be evidence, never a short-circuit — "
                f"that is how a stale row gets to overrule a live reading."
            )


validate_specs()

SIGNAL_NAMES: tuple[str, ...] = tuple(s.name for s in SIGNALS)
LOAD_BEARING: tuple[str, ...] = tuple(s.name for s in SIGNALS if s.load_bearing)
DECISIVE_SIGNALS: tuple[str, ...] = tuple(s.name for s in SIGNALS if s.decisive)

_BY_NAME = {s.name: s for s in SIGNALS}


def spec_for(name: str) -> SignalSpec:
    """The spec for one signal. Raises on an unknown name; never invents one."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"unknown signal {name!r} — the signal set is declared in "
            f"{__name__}.SIGNALS and nowhere else. Add it THERE (with what it "
            f"reads, its healthy pole, and why it is or is not load-bearing) "
            f"rather than introducing a criterion at a call site."
        ) from None


# EOF
