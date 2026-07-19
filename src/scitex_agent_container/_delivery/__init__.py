"""VERIFIED agent-to-agent delivery — a send that reports what it actually knows.

Agents talk by pasting into each other's tmux panes and learn NOTHING about
whether it worked. ``tmux send-keys`` into a session that does not exist prints
"can't find pane" to a stderr nobody reads and exits 0, so four completely
different outcomes are indistinguishable from the sender's side:

1. **TARGET DEAD** — the session does not exist. An operator coordinated with
   ``tui-dotfiles`` for hours; every message vanished and every one was reported
   as delivered.
2. **TARGET WEDGED** — the pane exists and nothing ever submits.
3. **TEXT SITS UNSUBMITTED IN THE COMPOSER** — the message landed (confirmed by
   reading the pane) and the agent stayed idle; a bare Enter started it working
   immediately. A send is NOT complete until the newline actually submits.
4. **THE VERIFICATION ITSELF LIES** — a pane grepped for a prose fragment
   returned nothing about a message that HAD arrived, because the TUI re-rendered
   and wrapped it.

This package makes each of those a NAMED, SEPARATELY REPORTED signal, and — the
part that matters most — keeps "I could not tell" as a first-class answer rather
than rounding it to one of the poles.

* :mod:`._spec` declares the signals, which are load-bearing, which are decisive
  (none are, and it says why). Validated at import.
* :mod:`._state` is the frozen dataclass a send returns, every signal
  ``bool | None``, with reasons and the RAW captures attached.
* :mod:`._assess` is THE single pure fold: True / False / None.
* :mod:`._token` is the arrival matcher — a short injected token against a
  flattened pane, which is the fix for mode 4.
* :mod:`._route` proves the target exists before anything is sent (mode 1), and
  refuses to convict off an enumeration that could not have shown presence.
* :mod:`._deliver` wires it together: ONE verb, two strategies — the existing
  ``sac agents send`` resume path when the agent has a recorded session id, and
  the verified tmux path for TUI agents, which is the population that matters.

The submit-verification (mode 3) is NOT new code: it reuses
``runtimes._tui_compose.verify_submit_by_advancement``, the idle-gated Enter with
bounded retry that already fixed this exact drop on the boot path. Composing the
proven part beats writing a second one that can drift from it.
"""

from __future__ import annotations

from ._assess import (
    EXIT_DELIVERED,
    EXIT_NO_ROUTE,
    EXIT_REFUTED,
    EXIT_UNKNOWN,
    EXIT_UNSUBMITTED,
    DeliveryAssessment,
    assess_delivery,
)
from ._deliver import (
    DEFAULT_ARRIVAL_TIMEOUT_S,
    DEFAULT_IDLE_WAIT_S,
    DEFAULT_MAX_RESENDS,
    DEFAULT_POLL_S,
    deliver,
)
from ._route import (
    STRATEGY_SDK,
    STRATEGY_TUI,
    TUI_SESSION_PREFIX,
    Route,
    list_tmux_sessions,
    read_agent_session_id,
    resolve_route,
)
from ._spec import (
    DELIVERY_LOAD_BEARING,
    DELIVERY_SIGNAL_NAMES,
    DELIVERY_SIGNALS,
    OBSERVATION_DIRECT,
    OBSERVATION_INFERRED,
    DeliverySignalSpec,
    delivery_spec_for,
    validate_delivery_specs,
)
from ._state import DeliveryState
from ._token import (
    DELIVERY_TOKEN_BYTES,
    flatten_pane,
    format_payload,
    make_token,
    pane_contains_token,
)

__all__ = [
    "DEFAULT_ARRIVAL_TIMEOUT_S",
    "DEFAULT_IDLE_WAIT_S",
    "DEFAULT_MAX_RESENDS",
    "DEFAULT_POLL_S",
    "DELIVERY_LOAD_BEARING",
    "DELIVERY_SIGNALS",
    "DELIVERY_SIGNAL_NAMES",
    "DELIVERY_TOKEN_BYTES",
    "EXIT_DELIVERED",
    "EXIT_NO_ROUTE",
    "EXIT_REFUTED",
    "EXIT_UNKNOWN",
    "EXIT_UNSUBMITTED",
    "OBSERVATION_DIRECT",
    "OBSERVATION_INFERRED",
    "STRATEGY_SDK",
    "STRATEGY_TUI",
    "TUI_SESSION_PREFIX",
    "DeliveryAssessment",
    "DeliverySignalSpec",
    "DeliveryState",
    "Route",
    "assess_delivery",
    "deliver",
    "delivery_spec_for",
    "flatten_pane",
    "format_payload",
    "list_tmux_sessions",
    "make_token",
    "pane_contains_token",
    "read_agent_session_id",
    "resolve_route",
    "validate_delivery_specs",
]

# EOF
