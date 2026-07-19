"""ONE state shape for an agent, with True / False / **None** everywhere.

The root-cause fix for the 2026-07-17/18 fleet incident, and the operator's own
design (Telegram 3150-3161):

    「True (rc=0), False, None を区別しないからおかしいのでは？」
    「基準がたくさんあるじゃん？それらを全部 dataclass で持てばいいんだよ。
      ロジックがおかしければそれでわかるでしょ？」
    「内部でごちゃごちゃやるからおかしくなるじゃんね」

The signals were never the problem. That night's own restart log printed
``delivery[unknown], process[dead], heartbeat[alive], registry[unknown]`` —
already tri-state, already named, already correct — and the verdict collapsed
anyway. What was wrong was the COMBINING, hidden at every call site, each folding
whatever subset it happened to hold. So:

* :mod:`._spec` declares the signal set, which signals are load-bearing, and
  which are decisive. One table. Adding a criterion is a spec change.
* :mod:`._state` is the frozen dataclass every verb returns for a peer's state —
  always the same shape, every signal ``Optional[bool]``, RAW captures attached.
* :mod:`._assess` is THE single pure fold. True / False / None, and a None names
  which signal it could not read.
* :mod:`._journal` archives the raw observations so a verdict can be
  re-examined later instead of merely believed.
* :mod:`._observe` takes the live reading; :mod:`._adapt` projects the auth-heal
  detector's output into the same shape.

Two properties are the whole point:

**Silence becomes a value.** A missing agent renders as an all-``None``
:meth:`.AgentState.unknown` row that assesses UNKNOWN and exits 2 — not as no row
at all. An agent absent from an enumeration used to produce no output, and no
output read as fine; that is how a login-expired agent sat unnoticed.

**Disagreement becomes visible.** ``sac agents auth-status`` and ``sac agents
list``, asked minutes apart on one host, returned different populations — 12
agents versus 11, with a live tmux session and a live pid on an agent the
registry called ``defined``. Under one always-returned state that is a single row
showing ``is_tmux_live=True`` next to ``is_registry_active=False``, instead of two
tools contradicting each other in the dark.
"""

from __future__ import annotations

from ._adapt import states_from_detection
from ._assess import EXIT_FALSE, EXIT_TRUE, EXIT_UNKNOWN, Assessment, assess
from ._journal import (
    DEFAULT_MAX_CAPTURE_BYTES,
    DEFAULT_MAX_JOURNAL_BYTES,
    JournalWrite,
    append_state,
    journal_path,
    mark_truncated,
    read_journal,
)
from ._observe import DEFAULT_INTERVAL, observe_agent, observe_fleet
from ._spec import (
    DECISIVE_SIGNALS,
    LOAD_BEARING,
    OBSERVATION_DIRECT,
    OBSERVATION_INFERRED,
    SIGNAL_NAMES,
    SIGNALS,
    SignalSpec,
    spec_for,
    validate_specs,
)
from ._state import AgentState

__all__ = [
    "DECISIVE_SIGNALS",
    "DEFAULT_INTERVAL",
    "DEFAULT_MAX_CAPTURE_BYTES",
    "DEFAULT_MAX_JOURNAL_BYTES",
    "EXIT_FALSE",
    "EXIT_TRUE",
    "EXIT_UNKNOWN",
    "LOAD_BEARING",
    "OBSERVATION_DIRECT",
    "OBSERVATION_INFERRED",
    "SIGNALS",
    "SIGNAL_NAMES",
    "AgentState",
    "Assessment",
    "JournalWrite",
    "SignalSpec",
    "append_state",
    "assess",
    "journal_path",
    "mark_truncated",
    "observe_agent",
    "observe_fleet",
    "read_journal",
    "spec_for",
    "states_from_detection",
    "validate_specs",
]

# EOF
