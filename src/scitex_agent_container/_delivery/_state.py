"""``DeliveryState`` — the ONE shape every send returns about its own outcome.

The constitution's §2 rule applied to the act of sending. :mod:`.._agentstate`
fixed "what state is this peer in?"; a send was still answering "did it work?"
with nothing at all — ``tmux send-keys`` returns 0 for a pane that does not
exist, and the caller learned NOTHING.

Every signal is ``bool | None``: **True / False / None, where None means COULD NOT
DETERMINE.** That third value is the entire point, and delivery is where it bites
hardest, because all four failure modes are indistinguishable from the sender's
side unless the shape can hold "I could not tell":

* the target was DEAD (session never existed, every message vanished, all of them
  reported as delivered);
* the target was WEDGED (pane exists, nothing ever submits);
* the text SAT UNSUBMITTED in the composer (it arrived, and the agent stayed idle
  until a bare Enter started it);
* the VERIFICATION ITSELF LIED (a prose grep returned 0 for a message that had in
  fact arrived — the TUI had re-rendered and wrapped it).

The fourth is the reason this type refuses to store only a verdict. ``raw``
carries what was actually SEEN — the pane before the send, the pane after the
paste, the pane after the submit — so a disputed outcome can be re-examined
against the bytes rather than re-argued from a summary. A summary is a claim you
cannot check later.

NO COMBINING LOGIC LIVES HERE
    This type HOLDS signals. It never folds them. The single fold is
    :func:`._assess.assess_delivery`, and there is exactly one of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from ._spec import DELIVERY_SIGNAL_NAMES, DELIVERY_SIGNALS, delivery_spec_for

__all__ = ["DeliveryState"]


@dataclass(frozen=True)
class DeliveryState:
    """Every criterion sac has about ONE delivery, each True / False / None.

    Construct it with whatever was actually observed and leave the rest ``None``
    — a signal that was not read is ``None`` by default, which is the honest
    value, and it is why the default constructor cannot accidentally claim a
    successful send. There is deliberately no bare ``bool`` anywhere in this type.
    """

    agent: str

    #: Which strategy was used: ``"sdk"`` (the existing ``sac agents send``
    #: resume path) or ``"tui"`` (verified tmux paste + submit). ONE verb, two
    #: strategies — and the reader must always be able to tell which one ran,
    #: because the two have different blind spots and an outcome is only
    #: interpretable against the way it was obtained.
    strategy: str = ""

    #: The short token injected into the payload and matched back out of the
    #: pane. Recorded so a human can grep the peer's scrollback for the SAME
    #: token later and independently confirm — or refute — this row.
    token: str = ""

    # --- the signals. Order matches the spec table. --------------------------
    is_route_resolved: bool | None = None
    is_payload_delivered: bool | None = None
    is_payload_submitted: bool | None = None
    is_pane_readable: bool | None = None
    is_target_busy_before: bool | None = None
    is_login_banner_before: bool | None = None

    #: Per-signal WHY. A ``None`` that does not say why it is None is a shrug
    #: wearing a type — the reader still cannot tell "the pane was unreadable"
    #: from "nobody looked", and those send them to different places.
    reasons: Mapping[str, str] = field(default_factory=dict)

    #: The RAW observations, keyed by the ``evidence`` names the spec declares
    #: (``pane_before``, ``pane_after_paste``, ``tmux_sessions``, …). WHOLE
    #: captures, not tails: the wrapped-prose false negative was only ever
    #: diagnosable because someone still had the full screen.
    raw: Mapping[str, str] = field(default_factory=dict)

    #: When the send was attempted (unix seconds). ``None`` when the caller did
    #: not supply one — never defaulted to "now", which would date an old
    #: reading to the moment it was rendered.
    observed_at: float | None = None

    #: Seconds the whole verified send took, so a budget can be tuned against
    #: measurements instead of guesses. The 25-second window that was misread as
    #: death is exactly the number this field exists to make visible.
    elapsed: float | None = None

    #: Who produced this row (``"sac agents deliver"``, …).
    observer: str = ""

    def __post_init__(self) -> None:
        for name in DELIVERY_SIGNAL_NAMES:
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(
                    f"{name} must be True, False or None — got {value!r} "
                    f"({type(value).__name__}). A signal is never a truthy "
                    f"string or an int: 'could not determine' is a first-class "
                    f"answer here and must be spelled None, and a non-bool would "
                    f"quietly evaluate as one of the poles."
                )
        for key in self.reasons:
            delivery_spec_for(key)  # raises, with guidance, on a typo
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))

    # --- construction --------------------------------------------------------

    @classmethod
    def unknown(cls, agent: str, reason: str, **kwargs: Any) -> "DeliveryState":
        """A send we could not evaluate AT ALL — every signal ``None``, with WHY.

        The constructor for "we did not even get far enough to try". It assesses
        to UNKNOWN and exits 2, which is the honest answer and, crucially, NOT
        the answer the old path gave: ``tmux send-keys`` into a nonexistent pane
        returned success, so an unattempted delivery and a completed one spelled
        the same thing.
        """
        return cls(
            agent=agent,
            reasons={name: reason for name in DELIVERY_SIGNAL_NAMES},
            **kwargs,
        )

    def with_signal(
        self, name: str, value: bool | None, reason: str = "", **evidence: str
    ) -> "DeliveryState":
        """A copy with one signal set, its reason recorded and its raw kept.

        The builder the sender uses per step, so a signal and the evidence it was
        read from can never be stored apart from each other.
        """
        delivery_spec_for(name)
        reasons = dict(self.reasons)
        if reason:
            reasons[name] = reason
        raw = dict(self.raw)
        raw.update(evidence)
        return replace(self, reasons=reasons, raw=raw, **{name: value})

    # --- reading -------------------------------------------------------------

    def signals(self) -> dict[str, bool | None]:
        """Every signal by name, including the ones that are ``None``.

        ALWAYS the full set — never only the ones that were read. A mapping that
        omits its unknowns cannot distinguish "we checked and it was fine" from
        "we never looked", which is precisely the pair that must never spell the
        same thing.
        """
        return {name: getattr(self, name) for name in DELIVERY_SIGNAL_NAMES}

    def reason_for(self, name: str) -> str:
        delivery_spec_for(name)
        return self.reasons.get(name, "")

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape. Carries EVERY RAW SIGNAL — the exit code is elsewhere.

        The signals appear exactly as observed, each with its reason and its spec
        metadata, and the summarising happens strictly at the exit code. A
        consumer can therefore disagree with our aggregation and recompute its
        own, which is impossible against a bare status string.
        """
        return {
            "agent": self.agent,
            "strategy": self.strategy,
            "token": self.token,
            "observed_at": self.observed_at,
            "elapsed": self.elapsed,
            "observer": self.observer,
            "signals": {
                spec.name: {
                    "value": getattr(self, spec.name),
                    "reason": self.reasons.get(spec.name, ""),
                    "healthy": spec.healthy,
                    "load_bearing": spec.load_bearing,
                    "decisive": spec.decisive,
                    "observation": spec.observation,
                    "reads": spec.reads,
                }
                for spec in DELIVERY_SIGNALS
            },
            "raw": dict(self.raw),
        }


# EOF
