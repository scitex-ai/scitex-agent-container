"""READ-ONLY detection: which live TUI agents are CORROBORATED login-expired.

Reuses the ``sac agents auth-status`` detector as the SINGLE SOURCE OF TRUTH for
the near-prompt + distance-frozen matcher (2-run corroboration): an agent is
flagged only when a system auth banner sits directly above its prompt AND stays
frozen across two captures ``--interval`` apart. A banner that moved — the agent
is producing output, working or merely QUOTING the incident — is never flagged,
so a false positive can never bounce a working agent and destroy its context.

ALL THREE VERDICTS LEAVE THIS MODULE
    The matcher answers ok / auth_failed / unknown, and :class:`DetectionOutcome`
    carries all three out. Only ``auth_failed`` authorises a restart — absence of
    evidence is not evidence of a wedge — but ``unknown`` is a FINDING, not a
    value to drop: an agent nobody could read is exactly the agent nobody is
    watching. A detector that returned the wedged names alone left "we observed
    nothing at all" and "we observed everything and it was fine" spelling the
    same empty answer, and nothing downstream could tell them apart.

THE POPULATION IS THE ROSTER, NOT THE ENUMERATION
    :func:`capture_live_panes` can only key on tmux sessions it can SEE, so an
    agent whose session is gone never becomes a key and could not be reported as
    anything at all. :func:`registered_agents` supplies the independent
    population — the same fleet registry ``sac.fleet-reconcile`` sweeps — that
    the reading is checked against, so an agent missing from the reading becomes
    a VALUE rather than a silence. That also makes a BLIND read self-announcing:
    an enumeration that comes back empty because tmux itself failed now leaves
    the whole roster unaccounted for, instead of looking like a quiet fleet.

Detection performs NO token-rotating probe and writes nothing: it captures panes
and classifies them, full stop (the ``sac agents auth-status`` writer owns the
state.db cache; this consumer must not fight its cadence). The ONLY mutation in
the whole flow is the restart itself, in :mod:`._pass`.

The ``sac agents auth-status`` symbols are imported LAZILY (inside the
functions) so this module stays importable without pulling ``click`` at import
time, and so there is no import-time coupling to the CLI package.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_INTERVAL",
    "DetectionOutcome",
    "Roster",
    "capture_live_panes",
    "capture_live_panes_once",
    "detect_login_expired",
    "registered_agents",
]

#: Seconds between the two pane captures whose agreement defines "frozen".
#: Same default as ``sac agents auth-status --interval``.
DEFAULT_INTERVAL = 4.0


@dataclass(frozen=True)
class DetectionOutcome:
    """Every agent we looked at, in the THREE buckets the matcher produces.

    The buckets PARTITION the input: every key of ``captures`` lands in exactly
    one of them, so a caller can distinguish "clean" from "never read" without
    re-deriving it from row fields. Each is sorted by agent name, inherited from
    the rows they are built out of.

    ``auth_failed`` is the only bucket that may lead to a restart. ``unknown`` is
    the bucket this type exists for: it must never be restarted, and it must
    never be discarded either — an instrument silent about the agents it failed
    to measure is reporting good news about things it never saw.
    """

    auth_failed: tuple[str, ...] = ()
    ok: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class Roster:
    """The REGISTERED agent population — or an honest refusal to claim one.

    ``readable=False`` means the fleet registry could not be enumerated, and it
    is deliberately NOT the same value as an empty registry. "No agents are
    registered" and "we cannot tell which agents are registered" lead to
    opposite conclusions about a thin pane reading: the first says nothing is
    missing, the second says we have no idea what is missing. Collapsing them
    would let the roster check certify a fleet it never managed to look up.

    Mirrors :class:`.._reconcile._budget.HistoryRead`, which draws exactly this
    distinction for the restart history.
    """

    names: tuple[str, ...] = ()
    readable: bool = True
    detail: str = ""


def detect_login_expired(
    captures: "dict[str, tuple[str | None, str | None]]",
) -> DetectionOutcome:
    """Classify two-capture panes into OK / AUTH-FAILED / UNKNOWN.

    ``captures`` maps ``agent -> (pane_run1, pane_run2)``. Delegates to the real
    ``evaluate_agents`` corroboration and keeps ALL THREE of its verdicts:
    ``auth_failed`` (a system auth banner frozen above the prompt across both
    reads), ``ok`` (the banner moved, or there was none), and ``unknown`` (the
    pane could not be read). Pure: no tmux, no I/O, so it is unit-testable
    against captured panes without mocks.

    Only ``auth_failed`` authorises a restart. ``unknown`` never does — absence
    of evidence is not evidence of a wedge, and bouncing an agent on a reading
    we failed to take would destroy the context of something that was working
    fine — but it is returned rather than dropped, so the pass can report it.
    """
    from ..cli_pkg._auth_status import (
        VERDICT_AUTH_FAILED,
        VERDICT_UNKNOWN,
        evaluate_agents,
    )

    rows = evaluate_agents(captures)  # sorts by agent name
    return DetectionOutcome(
        auth_failed=tuple(
            r["agent"] for r in rows if r["verdict"] == VERDICT_AUTH_FAILED
        ),
        ok=tuple(
            r["agent"]
            for r in rows
            if r["verdict"] not in (VERDICT_AUTH_FAILED, VERDICT_UNKNOWN)
        ),
        unknown=tuple(r["agent"] for r in rows if r["verdict"] == VERDICT_UNKNOWN),
    )


def registered_agents(specs_dir: "Path | None" = None) -> Roster:
    """The fleet registry's agent names — the population a pass must account for.

    Reuses ``sac.fleet-reconcile``'s enumeration
    (:func:`.._reconcile._pass.fleet_spec_paths`) instead of inventing a second
    roster, so the two sweeps can never disagree about which agents exist and
    the one ``SCITEX_AGENT_CONTAINER_AGENTS_DIR`` override redirects both.

    A registry we cannot enumerate yields ``readable=False``, never an empty
    roster. An empty roster would quietly assert that no agent is missing, which
    is the strongest claim available and the last one earned by having seen
    nothing.
    """
    from .._reconcile._pass import fleet_agents_dir, fleet_spec_paths

    root = specs_dir if specs_dir is not None else fleet_agents_dir()
    if not root.is_dir():
        return Roster(
            readable=False,
            detail=f"the fleet registry {root} is not a readable directory, so "
            f"we cannot know which agents SHOULD have a live session",
        )
    # stx-allow: fallback (reason: a revoked/unreadable registry must render as UNKNOWN, never as an empty roster that would silently certify "nobody is missing"; the OS's own message is carried into the report)
    try:
        names = tuple(spec.parent.name for spec in fleet_spec_paths(root))
    except OSError as exc:
        return Roster(
            readable=False,
            detail=f"could not enumerate the fleet registry {root}: {exc}",
        )
    return Roster(names=names, detail=f"{len(names)} agent(s) registered in {root}")


def capture_live_panes(
    interval: float = DEFAULT_INTERVAL,
) -> "dict[str, tuple[str | None, str | None]]":
    """Capture every live ``tui-<agent>`` pane TWICE, ``interval`` apart.

    The live default seam for :func:`._pass.auth_heal_pass` — reuses the exact
    tmux enumeration + capture the ``sac agents auth-status`` command uses, so
    the two commands see the same fleet on the same tmux server. An uncapturable
    pane is ``None`` (the honest "could not read"), which the matcher maps to
    UNKNOWN — never a false AUTH-FAILED.

    The KEYS here are the live sessions and nothing else, which makes this a
    reading of the fleet rather than the fleet itself: an agent whose session is
    gone cannot appear, however wrong its absence is. Squaring this reading
    against the :func:`registered_agents` roster is the pass's job, and that is
    what turns such an absence into a reportable value.
    """
    from ..cli_pkg._auth_status import _agent_of, _capture, _list_tui_sessions

    sessions = _list_tui_sessions()
    run1 = {_agent_of(s): _capture(s) for s in sessions}
    time.sleep(max(0.0, interval))
    run2 = {_agent_of(s): _capture(s) for s in sessions}
    return {name: (run1.get(name), run2.get(name)) for name in run1}


def capture_live_panes_once() -> "dict[str, str | None]":
    """Capture every live ``tui-<agent>`` pane ONCE — ``{agent: pane_or_None}``.

    The live seam for the NEAR-PROMPT discriminator
    (:mod:`._nearprompt`), which judges the CURRENT UI state and so needs
    exactly one reading. Deliberately not :func:`capture_live_panes`: a second
    capture exists only to be compared against the first, and it is precisely
    that comparison — the freeze test — that misclassifies an animating-but-
    wedged agent as healthy. Taking one reading is not a shortcut here, it is
    the correction.

    Reuses the same tmux enumeration + capture as ``sac agents auth-status``, so
    both commands see the same fleet on the same tmux server. An uncapturable
    pane is ``None``, the honest "could not read" that the discriminator maps to
    UNKNOWN — never to a false LOGIN-REQUIRED and never to a false OK.
    """
    from ..cli_pkg._auth_status import _agent_of, _capture, _list_tui_sessions

    return {_agent_of(s): _capture(s) for s in _list_tui_sessions()}
