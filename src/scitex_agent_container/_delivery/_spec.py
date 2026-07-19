"""THE DELIVERY SPEC: which signals a send produces, and which ones may convict.

The sibling of :mod:`.._agentstate._spec`, for the other half of the question.
``_agentstate`` answers "what state is this peer in?"; this table answers "did the
thing I sent it actually ARRIVE, and did it actually RUN?" — and those are not the
same question, because a perfectly healthy agent can sit forever with a message
pasted into its composer that nobody ever submitted.

WHY EVERY DELIVERY SIGNAL IS NON-DECISIVE
-----------------------------------------
``_agentstate`` grants ``is_process_alive`` decisiveness because the process table
is read first-hand and cannot be wrong about absence. NOTHING here earns that.
Every delivery reading is taken through a pane capture or a subprocess exit
status, and each of those has a measured way to lie:

* a pane capture aimed at the wrong tmux server reports an empty pane, and empty
  reads as clean (``_runners/_tmux/pane_capture`` targets a DEDICATED ``sac``
  server, NOT the default server the live fleet runs on — reading the wrong
  server's emptiness as death is the exact shape of the 2026-07-14 false DEAD);
* a prose match against a re-rendered, soft-wrapped TUI returns 0 for text that
  DID arrive (measured 2026-07-18: an operator grepped a pane for a sentence
  fragment, got nothing, and reported "not delivered" about a message sitting on
  screen);
* a subprocess exit status is a claim about a process, not an observation of the
  peer's session.

So no signal below is ``decisive``, and :func:`validate_delivery_specs` enforces
the same rule the sibling table does — decisiveness requires DIRECT observation —
so nobody can grant it later without also changing how the signal is read. The
practical consequence is deliberate: this subsystem can report "I could not tell",
and it will never authorise a destructive remedy off a reading it did not take.

WHAT EACH FLAG MEANS
--------------------
Identical semantics to :mod:`.._agentstate._spec` — ``healthy`` names the good
pole (it is NOT always ``True``), ``load_bearing`` decides whether the signal
participates in the verdict, ``decisive`` allows a short-circuit past unknowns,
and ``evidence`` names the raw captures the signal was read out of.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DELIVERY_LOAD_BEARING",
    "DELIVERY_SIGNALS",
    "DELIVERY_SIGNAL_NAMES",
    "OBSERVATION_DIRECT",
    "OBSERVATION_INFERRED",
    "DeliverySignalSpec",
    "delivery_spec_for",
    "validate_delivery_specs",
]

#: The sensor physically read the thing itself, in this process, just now.
OBSERVATION_DIRECT = "direct"

#: The value came from an exit status, a cache, a declaration, or a deduction.
#: Perfectly good evidence; never grounds for a decisive verdict.
OBSERVATION_INFERRED = "inferred"


@dataclass(frozen=True)
class DeliverySignalSpec:
    """One delivery criterion, and everything the fold needs to know about it."""

    name: str
    reads: str
    healthy: bool
    load_bearing: bool
    why: str
    decisive: bool = False
    observation: str = OBSERVATION_INFERRED
    evidence: tuple[str, ...] = ()


DELIVERY_SIGNALS: tuple[DeliverySignalSpec, ...] = (
    DeliverySignalSpec(
        name="is_route_resolved",
        reads=(
            "whether a delivery route to this agent exists — a tui-<agent> "
            "session on the DEFAULT tmux server the live fleet runs on, or a "
            "recorded session_id for the SDK send path"
        ),
        healthy=True,
        load_bearing=True,
        decisive=False,
        observation=OBSERVATION_DIRECT,
        evidence=("tmux_sessions", "session_id_path"),
        why=(
            "LOAD-BEARING: with no route there is no delivery, and this is the "
            "mode that cost the most — an operator coordinated with tui-dotfiles "
            "for HOURS while every `tmux send-keys` returned 'can't find pane' "
            "and each message was reported as delivered. NOT decisive, and the "
            "resolver must render None rather than False whenever the "
            "enumeration itself came back EMPTY: an empty tmux session list is "
            "exactly what a process in a different mount namespace sees of the "
            "host's tmux, so emptiness is a statement about the observer, not "
            "about the fleet. Only an enumeration that DID show other sessions "
            "is capable of showing this one's absence, and only then may absence "
            "read False."
        ),
    ),
    DeliverySignalSpec(
        name="is_payload_delivered",
        reads=(
            "whether the payload's unique token was observed at the target — "
            "found in the FLATTENED pane capture (TUI path), or implied by a "
            "clean return from the agent's own send endpoint (SDK path)"
        ),
        healthy=True,
        load_bearing=True,
        decisive=False,
        observation=OBSERVATION_INFERRED,
        evidence=("pane_after_paste", "send_detail"),
        why=(
            "LOAD-BEARING: text that never landed cannot be acted on. INFERRED "
            "rather than direct because the SDK strategy reads this off a "
            "subprocess outcome rather than off the peer's screen, and a signal's "
            "observation grade must describe its WEAKEST reader — otherwise one "
            "strategy's inference inherits the other strategy's authority. Never "
            "decisive: the matcher runs against a live TUI that re-renders and "
            "soft-wraps, and a negative there has already been wrong in "
            "production."
        ),
    ),
    DeliverySignalSpec(
        name="is_payload_submitted",
        reads=(
            "whether the payload was actually SUBMITTED as a turn — the live "
            "compose box observed to advance (empty) after Enter on the TUI "
            "path, or the turn returned on the SDK path"
        ),
        healthy=True,
        load_bearing=True,
        decisive=False,
        observation=OBSERVATION_INFERRED,
        evidence=("pane_after_submit",),
        why=(
            "LOAD-BEARING, and THE signal this whole verb exists to add. Measured "
            "2026-07-18: a message landed in a peer's composer (confirmed by "
            "reading the pane) and the agent stayed idle; a bare Enter into that "
            "pane started it working immediately. A send is therefore NOT "
            "complete when the text arrives — arrival and submission are two "
            "different facts and the old path only ever checked the first, which "
            "is why 'delivered' and 'ignored' were indistinguishable. INFERRED + "
            "non-decisive for the same reason as is_payload_delivered."
        ),
    ),
    DeliverySignalSpec(
        name="is_pane_readable",
        reads="whether the target's pane could be captured at all",
        healthy=True,
        load_bearing=False,
        observation=OBSERVATION_DIRECT,
        evidence=("pane_before", "pane_after_paste"),
        why=(
            "EVIDENCE ONLY — it is the PRECONDITION of the two load-bearing "
            "readings above, not a competitor to them. When the pane cannot be "
            "read they are already None and the verdict is already UNKNOWN, so "
            "promoting this would double-count one failure. It is carried "
            "because it tells the reader WHICH KIND of unknown they have: 'the "
            "screen was unreadable' sends an operator somewhere completely "
            "different from 'the screen was fine and the token never appeared'. "
            "It is also None, never False, on the SDK path — that path has no "
            "pane, and 'there was nothing to read' is not 'we failed to read it'."
        ),
    ),
    DeliverySignalSpec(
        name="is_target_busy_before",
        reads=(
            "whether the pane showed an in-progress marker (spinner / 'esc to "
            "interrupt') on the capture taken BEFORE the send"
        ),
        healthy=False,
        load_bearing=False,
        observation=OBSERVATION_DIRECT,
        evidence=("pane_before",),
        why=(
            "EVIDENCE ONLY. A busy agent is a WORKING agent — the healthiest "
            "thing on the fleet — and delivery to it succeeds normally because "
            "the text queues in the composer and the submit step waits for idle. "
            "Making this refute would flag exactly the agents that are fine. It "
            "is carried because it is the single best explanation for a slow "
            "delivery, and an operator who cannot see it will misread a long "
            "wait as a wedge (25 seconds was misread as death once already; a "
            "UserPromptSubmit hook alone can legitimately take 30)."
        ),
    ),
    DeliverySignalSpec(
        name="is_login_banner_before",
        reads=(
            "whether an auth banner ('Login expired') sat above the prompt on "
            "the single capture taken BEFORE the send"
        ),
        healthy=False,
        load_bearing=False,
        observation=OBSERVATION_DIRECT,
        evidence=("pane_before",),
        why=(
            "EVIDENCE ONLY, and deliberately so despite being load-bearing in "
            "the sibling table. There it is trusted because it is corroborated "
            "across TWO captures taken an interval apart — a banner that MOVED "
            "means the agent is working or merely quoting the incident. A send "
            "takes ONE pre-capture, so this reading is UNCORROBORATED, and an "
            "uncorroborated banner match must never be allowed to refute a "
            "delivery. Surfaced loudly all the same: it is usually the reason a "
            "submitted turn produces nothing, and the operator should be told to "
            "go look at `sac agents auth-status` rather than resend."
        ),
    ),
)


def validate_delivery_specs(
    specs: "tuple[DeliverySignalSpec, ...]" = DELIVERY_SIGNALS,
) -> None:
    """Refuse a spec table that would let an INFERENCE convict.

    Called at module scope, so a bad table fails at IMPORT rather than shipping
    and being discovered by an operator acting on its verdict. Same three
    invariants as :func:`.._agentstate._spec.validate_specs`, each with a real
    failure behind it: a duplicate name lets one signal silently shadow another
    in the lookup; a decisive signal that is not load-bearing would
    short-circuit a verdict it takes no part in; and a decisive signal read from
    an exit status is precisely the "confident answer about a thing we never
    observed" this package exists to end.
    """
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise ValueError(
                f"duplicate delivery signal {spec.name!r} — one entry would "
                f"silently shadow the other, and half the callers would be "
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
                f"must be a thing we read ourselves, just now. A subprocess exit "
                f"status or a cached verdict may be evidence, never a "
                f"short-circuit."
            )


validate_delivery_specs()

DELIVERY_SIGNAL_NAMES: tuple[str, ...] = tuple(s.name for s in DELIVERY_SIGNALS)
DELIVERY_LOAD_BEARING: tuple[str, ...] = tuple(
    s.name for s in DELIVERY_SIGNALS if s.load_bearing
)

_BY_NAME = {s.name: s for s in DELIVERY_SIGNALS}


def delivery_spec_for(name: str) -> DeliverySignalSpec:
    """The spec for one delivery signal. Raises on an unknown name."""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(
            f"unknown delivery signal {name!r} — the signal set is declared in "
            f"{__name__}.DELIVERY_SIGNALS and nowhere else. Add it THERE (with "
            f"what it reads, its healthy pole, and why it is or is not "
            f"load-bearing) rather than introducing a criterion at a call site."
        ) from None


# EOF
