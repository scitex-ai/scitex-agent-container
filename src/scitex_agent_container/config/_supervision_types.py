"""Supervision-policy dataclasses: health, watchdog, autonomous drive, restart.

Extracted from :mod:`._types` to keep that module under the 512-line per-file
cap (it was already 523 lines before this change). The four blocks moved
together because they answer one question — "what keeps this agent running, and
for how long" — which is the same reason ``_acl_types``, ``_apptainer_spec``
and ``_provider_types`` already live beside it.

THE IMPORT SURFACE DOES NOT MOVE: every name here is re-exported from
``._types``, so ``from scitex_agent_container.config._types import HealthSpec``
is the SAME object it always was, defined next door. ``_explicit_fields`` reads
these via ``dataclasses.fields()`` to derive the required-key map, so a split
that changed the import path would silently change which spec keys are
mandatory.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HealthSpec:
    enabled: bool = False
    interval: int = 30
    timeout: int = 5
    method: str = "multiplexer-alive"


# Parsed for backward compat but not interpreted by runtime.
# Watchdog lifecycle is managed externally via hooks.
@dataclass
class WatchdogSpec:
    enabled: bool = False
    interval: float = 1.5
    resp_y_n: str = "1"
    resp_y_y_n: str = "2"
    resp_waiting: str = "/speak-and-call"


# F-CS3 — autonomous drive-until-done.
#
# claude-session runners do ONE turn and idle by default; multi-turn
# tasks have to wrap externally with a2a peer post-turn loops, and
# every project ends up rewriting that scaffolding. The autonomous
# block lets the runner natively:
#
#   1. Watch each assistant turn for a text match (``drive_until``);
#      hitting it exits the runner with code 0.
#   2. After ``idle_kick_after_s`` of no tool activity AND no match,
#      post ``kick_text`` so the conversation keeps moving.
#   3. Cap at ``max_turns`` to prevent runaway loops.
#
# Phase 1 (this dataclass + parser + validator) lands the schema so
# yamls can author the contract today; the runner-side enforcement
# (consume these fields in _runners.claude_session) lands in phase 2.
# An ``enabled`` row authored under the schema before phase 2 ships
# is harmless — the runner just ignores it for now.
@dataclass
class AutonomousSpec:
    enabled: bool = False
    drive_until: str = "DONE"
    max_turns: int = 50
    idle_kick_after_s: int = 120
    kick_text: str = "Continue. Print DONE when finished."


@dataclass
class RestartSpec:
    policy: str = "never"  # never | on-failure | always
    max_retries: int = 3
    backoff_initial: int = 30
    backoff_max: int = 300
    backoff_multiplier: int = 2
    # Inode-hygiene opt-in (sac-runtime-state-hygiene incident): when
    # True AND ``policy == "never"``, a CLEAN terminal ``sac agents stop``
    # prunes this agent's runtime dir + overlay so ephemeral capsules
    # don't accumulate one-per-run forever. EXPLICIT opt-in is required
    # (default False) — ``policy`` itself DEFAULTS to "never", so a
    # persistent coordinator that merely omits a ``restart:`` block must
    # NOT be pruned; only specs that deliberately set this flag are.
    prune_on_stop: bool = False


__all__ = ["AutonomousSpec", "HealthSpec", "RestartSpec", "WatchdogSpec"]
