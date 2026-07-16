"""One-way code sync, centre → remote (``sac host sync``).

The centre (ywata-note-win) is the BRAIN: all config and authority live
there, and code flows one way — centre to remote. A remote never
originates code. Until now sac could LAUNCH an agent on a peer but could
not say WHICH CODE ran there, and nothing announced the difference:
Spartan's checkout sat FIVE RELEASES STALE (v0.21.14 while develop was
v0.21.20) with no post-merge pull anywhere, and it was found by hand.

This package closes that gap in two halves, and the first half is the
product:

* :func:`check_peer` — READ-ONLY drift detection. Mutates nothing, exits
  non-zero on drift so a cron can alarm on it. This is the half that
  would have caught both of 2026-07-14's silent incidents.
* :func:`sync_peer` — the fast-forward-only remedy, behind loud
  preconditions.

The invariants, each one paid for:

* **AHEAD is an alarm, not a merge.** A remote holding commits the
  centre lacks has already violated the one-way property. sac neither
  merges them back (that would make the remote a source of truth) nor
  discards them (that would destroy them). It prints them and stops.
* **UNKNOWN is not clean.** A failed probe, an unreachable peer, an
  unreadable CI state — none of these authorise a mutation.
* **Verify by symbol, never by version string.** Version strings lie;
  loaded module paths and imported symbols do not.
* **Never silent.** There is no quiet success path: a no-op still says
  what it verified.
"""

from ._alarm import AlarmOutcome, card_id_for, route_reports_to_cards
from ._apply import FastForwardResult, apply_fast_forward
from ._ci_guard import DEFAULT_REPO, CiState, CiVerdict, check_ci_idle
from ._model import GraphState, PeerSyncReport, SyncDecision, sync_decision
from ._peer_config import GENERATED_HEADER_MARK, is_generated, render_peer_config
from ._probe import probe_peer
from ._push_config import (
    ConfigVerdict,
    PushConfigResult,
    check_config_peer,
    master_config_sha,
    push_config_peer,
)
from ._sync import (
    Outcome,
    SyncResult,
    check_peer,
    exit_code_for,
    sync_peer,
    syncable_peers,
)

__all__ = [
    "DEFAULT_REPO",
    "GENERATED_HEADER_MARK",
    "AlarmOutcome",
    "CiState",
    "CiVerdict",
    "ConfigVerdict",
    "FastForwardResult",
    "GraphState",
    "Outcome",
    "PeerSyncReport",
    "PushConfigResult",
    "SyncDecision",
    "SyncResult",
    "apply_fast_forward",
    "card_id_for",
    "check_ci_idle",
    "check_config_peer",
    "check_peer",
    "exit_code_for",
    "is_generated",
    "master_config_sha",
    "probe_peer",
    "push_config_peer",
    "render_peer_config",
    "route_reports_to_cards",
    "sync_decision",
    "sync_peer",
    "syncable_peers",
]
