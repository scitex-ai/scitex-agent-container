"""``AgentState`` — the ONE shape every sac verb returns for a peer's state.

「これを dataclass で常に同じものを返すんです、相手の状態として」

Every field is ``Optional[bool]``: **True / False / None, where None means COULD
NOT DETERMINE.** That third value is the entire point. Every failure of the
2026-07-17/18 fleet incident was an UNKNOWN collapsed into a pole — a detector
that computed three verdicts and returned ``list[str]``, a ``systemctl show`` that
answers ``Result=success`` for a unit that does not exist, a transcript probe that
rendered a wrong path as ``0 bytes``, an enumeration whose emptiness read as a
healthy fleet.

THE SHAPE IS FIXED, SO ABSENCE BECOMES A VALUE
    Today every verb returns an ad-hoc shape — list columns, an auth-status
    table, a restart line — and an agent MISSING from one enumeration produces no
    row at all rather than an alarm. That is how a login-expired agent sat
    unnoticed: nothing was RED, because nothing was rendered. Here the fields
    always exist, so an agent nobody could read is a row of Nones
    (:meth:`AgentState.unknown`) that renders loudly. **Silence becomes a value.**

THE RAW OBSERVATIONS TRAVEL WITH THE VERDICT
    「状態とった時に全ログを取っておいてくださいね？」 — ``raw`` carries what was
    actually SEEN, not a summary of it: both full pane captures, the ps line with
    its start time, the transcript path that was inspected. Verdicts cannot be
    re-examined after the fact; captures can. Every diagnosis during the incident
    that reached a true answer did so because someone had kept raw text, and
    every one that went wrong had only summaries. A summary is a claim you cannot
    check later, which is why this dataclass refuses to store only summaries.

NO HIDDEN COMBINING LOGIC LIVES HERE
    This type HOLDS signals. It never folds them. The single fold is
    :func:`._assess.assess`, and there is exactly one of it, because the scattered
    per-call-site folding is the bug — not the signals, which were already
    tri-state, named and correct.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from ._spec import SIGNAL_NAMES, SIGNALS, spec_for

__all__ = ["AgentState"]

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class AgentState:
    """Every criterion sac has about one agent, each True / False / None.

    Construct it with whatever you actually observed and leave the rest ``None``
    — a signal you did not read is ``None`` by default, which is the honest
    value, and it is why the default constructor cannot accidentally claim
    health. There is deliberately no ``bool`` anywhere in this type.
    """

    agent: str

    # --- the signals. Order matches the spec table. --------------------------
    is_tmux_live: bool | None = None
    is_process_alive: bool | None = None
    is_login_required: bool | None = None
    is_at_idle_prompt: bool | None = None
    is_session_advancing: bool | None = None
    is_auth_probe_ok: bool | None = None
    is_inbox_reachable: bool | None = None
    is_heartbeat_fresh: bool | None = None
    is_registry_active: bool | None = None

    #: Per-signal WHY. A ``None`` that does not say why it is None is a shrug
    #: wearing a type — the reader still cannot tell "the pane was unreadable"
    #: from "nobody looked", and those send them to different places.
    reasons: Mapping[str, str] = field(default_factory=dict)

    #: The RAW observations, keyed by the ``evidence`` names the spec declares
    #: (``pane_run1``, ``ps_line``, ``session_path``, …). Whole captures, not
    #: tails: a tail slice is how an investigator watched a countdown widget
    #: change and nearly concluded "it is working" without seeing the content.
    raw: Mapping[str, str] = field(default_factory=dict)

    #: When the observation was taken (unix seconds), so a stored row can be
    #: aged. ``None`` when the caller did not supply one — never defaulted to
    #: "now", which would date an old reading to the moment it was rendered.
    observed_at: float | None = None

    #: Where this reading came from (``"sac agents state"``, ``"auth-heal"``, …).
    #: Two views disagreeing is the founding bug; a row that cannot say who
    #: produced it cannot be told apart from the row it contradicts.
    observer: str = ""

    def __post_init__(self) -> None:
        for name in SIGNAL_NAMES:
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
            spec_for(key)  # raises, with the spec's own guidance, on a typo
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))

    # --- construction --------------------------------------------------------

    @classmethod
    def unknown(cls, agent: str, reason: str, **kwargs: Any) -> "AgentState":
        """An agent we could not read AT ALL — every signal ``None``, with WHY.

        THIS IS THE MISSING-AGENT CONSTRUCTOR, and it is the reason absence stops
        being invisible. When an enumeration does not contain an agent the roster
        says exists, the honest answer is not to omit it (which renders as
        nothing, and nothing reads as fine) — it is a full row of Nones that
        assesses to UNKNOWN and exits 2. The population is the ROSTER; the
        enumeration is merely a reading of it.
        """
        return cls(
            agent=agent,
            reasons={name: reason for name in SIGNAL_NAMES},
            **kwargs,
        )

    def with_signal(
        self, name: str, value: bool | None, reason: str = "", **evidence: str
    ) -> "AgentState":
        """A copy with one signal set, its reason recorded and its raw kept.

        The builder an observer uses per probe, so a signal and the evidence it
        was read from can never be stored apart from each other.
        """
        spec_for(name)
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
        omits its unknowns is the ``list[str]`` bug again in dict costume: the
        caller cannot distinguish "we checked and it was fine" from "we never
        looked", which is precisely the pair that must never spell the same
        thing.
        """
        return {name: getattr(self, name) for name in SIGNAL_NAMES}

    def reason_for(self, name: str) -> str:
        spec_for(name)
        return self.reasons.get(name, "")

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape. Carries EVERY RAW SIGNAL — the exit code is elsewhere.

        「json で feedback を返せばいいんじゃないの？exit code はまとめた表現、
        信号はそのまま書く」 — so the signals appear here exactly as observed,
        each with its reason and its spec metadata (``healthy`` / ``load_bearing``
        / ``decisive`` / ``observation``), and the summarising happens strictly at
        the exit code. A consumer can therefore disagree with our aggregation and
        recompute its own, which is impossible against a bare status string.

        ``raw`` is included whole. :mod:`._journal` is what bounds it on disk, and
        it MARKS every truncation — a capture that looks complete but is not is
        worse than one that admits it was cut.
        """
        return {
            "agent": self.agent,
            "observed_at": self.observed_at,
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
                for spec in SIGNALS
            },
            "raw": dict(self.raw),
        }


# EOF
