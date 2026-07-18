"""READ-ONLY detection: which live TUI agents are CORROBORATED login-expired.

Reuses the ``sac agents auth-status`` detector as the SINGLE SOURCE OF TRUTH for
the near-prompt + distance-frozen matcher (2-run corroboration): an agent is
flagged only when a system auth banner sits directly above its prompt AND stays
frozen across two captures ``--interval`` apart. A banner that moved — the agent
is producing output, working or merely QUOTING the incident — is never flagged,
so a false positive can never bounce a working agent and destroy its context.

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

__all__ = ["DEFAULT_INTERVAL", "capture_live_panes", "detect_login_expired"]

#: Seconds between the two pane captures whose agreement defines "frozen".
#: Same default as ``sac agents auth-status --interval``.
DEFAULT_INTERVAL = 4.0


def detect_login_expired(
    captures: "dict[str, tuple[str | None, str | None]]",
) -> list[str]:
    """Corroborated login-expired agent NAMES (sorted), from two-capture panes.

    ``captures`` maps ``agent -> (pane_run1, pane_run2)``. Delegates to the real
    ``evaluate_agents`` corroboration and keeps ONLY the ``auth_failed`` verdict
    — never ``ok`` (banner moved / clean) and never ``unknown`` (pane could not
    be read: absence of evidence is not evidence of a wedge). Pure: no tmux, no
    I/O, so it is unit-testable against captured panes without mocks.
    """
    from ..cli_pkg._auth_status import VERDICT_AUTH_FAILED, evaluate_agents

    rows = evaluate_agents(captures)  # sorts by agent name
    return [r["agent"] for r in rows if r["verdict"] == VERDICT_AUTH_FAILED]


def capture_live_panes(
    interval: float = DEFAULT_INTERVAL,
) -> "dict[str, tuple[str | None, str | None]]":
    """Capture every live ``tui-<agent>`` pane TWICE, ``interval`` apart.

    The live default seam for :func:`._pass.auth_heal_pass` — reuses the exact
    tmux enumeration + capture the ``sac agents auth-status`` command uses, so
    the two commands see the same fleet on the same tmux server. An uncapturable
    pane is ``None`` (the honest "could not read"), which the matcher maps to
    UNKNOWN — never a false AUTH-FAILED.
    """
    from ..cli_pkg._auth_status import _agent_of, _capture, _list_tui_sessions

    sessions = _list_tui_sessions()
    run1 = {_agent_of(s): _capture(s) for s in sessions}
    time.sleep(max(0.0, interval))
    run2 = {_agent_of(s): _capture(s) for s in sessions}
    return {name: (run1.get(name), run2.get(name)) for name in run1}
