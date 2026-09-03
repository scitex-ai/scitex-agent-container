#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The PRE-LAUNCH gauntlet — everything that must hold before the runtime.

Extracted from ``_start.agent_start`` under the project's 512-line
per-file cap (same split as the sibling ``_start_preflight`` /
``_start_supervision`` / ``_start_failure_diag`` modules). ONE contiguous
region of ``agent_start`` moved verbatim: the two spec-sanity gates, the
board-identity check, the credential rotation, the START-TIME OVERRIDES
(session / resume id / ENGINE), the one-shot requirement, the spawn ACL
gate + lineage record, the ACL policy publish, the a2a port resolution,
and the telegrammer wake-wiring check.

WHY THESE BELONG TOGETHER: every one of them runs BEFORE the runtime is
built and before any forced stop, so a refusal here never tears down a
running agent it cannot bring back. That ordering is load-bearing — it is
why ``_rotate_to_healthy_account``'s docstring says "runs before
forced_stop / runtime build" — and giving the region a name makes it
checkable instead of incidental.

``spec.engines`` selection joins the region for exactly that reason: an
engine that cannot be honoured must refuse while the OLD agent is still
up, not after ``--force`` has already stopped it.
"""

from __future__ import annotations

from typing import Any

from ._engine_select import select_engine_at_start
from ._identity_drift import check_board_identity_at_launch
from ._layers_preflight import check_to_home_layers_at_launch
from ._spawn_gate import enforce_spawn_gate, persist_acl_policy
from ._start_preflight import (
    _check_spec_source_drift_at_launch,
    _rotate_to_healthy_account,
)
from ._a2a_port import resolve_a2a_port

__all__ = ["run_prelaunch"]


def run_prelaunch(
    config: Any,
    config_path: str,
    *,
    strict_drift: bool | None,
    session_override: str | None,
    resume_id_override: str | None,
    engine_override: str | None,
    probe_engine: bool | None,
    one_shot: bool,
    dry_run: bool,
) -> None:
    """Run every pre-runtime gate for ``config``, raising on the first refusal.

    Raises whatever the individual gates raise — a stale-spec refusal, a
    ``NoHealthyAccountError``, an ``UnknownEngineError`` /
    ``EngineNotHonourableError``, a ``SpawnDeniedError``, the one-shot
    ``RuntimeError``, or the telegrammer wake-wiring error. Returns
    ``None`` when every gate passed.
    """
    # TWO SPEC-SANITY GATES, refuse-by-default, each with its OWN named
    # override (operator ruling 2026-08-10 — never a blanket --force).
    # (1) spec source BEHIND/DIVERGED = a possibly STALE spec; escape hatch
    # ``--allow-stale-spec``. AHEAD / non-git / unreachable still start.
    # (2) undeclared ``to_home_layers``; escape hatch
    # ``--allow-undeclared-layers``, and the refusal itself is still gated
    # on the fleet migration (``_layers_preflight.ENFORCE_BY_DEFAULT``).
    # (2) is called HERE, once, not in the resolver a start invokes twice.
    _check_spec_source_drift_at_launch(config_path, config.name, strict_drift)
    check_to_home_layers_at_launch(config)

    # Launch-time BOARD IDENTITY check, same contract as the drift check
    # above: LOUD WARNING, never a block, never crashes a launch. An agent
    # whose name and SCITEX_TODO_AGENT_ID disagree is one process with two
    # identities — peers address it by one, it owns cards and polls its
    # inbox as the other — and every symptom is SILENT, because a card
    # query for the wrong spelling returns a well-formed empty list rather
    # than an error. Measured 2026-08-09: that hid a P1 from the agent
    # that owned it for over an hour. See :mod:`._identity_drift`.
    check_board_identity_at_launch(config)

    # CREDS-PHASE1 — auto-rotate ``spec.claude.account`` to a healthy
    # stored account when the pinned one's snapshot is EXPIRED/ABSENT.
    # Runs before forced_stop / runtime build so a "no healthy account"
    # error never tears down a running agent we cannot restart. Unpinned
    # agents (account="") are untouched: they continue to use the host
    # live ``.credentials.json`` via the existing bind. See
    # :func:`_rotate_to_healthy_account` for the contract.
    _rotate_to_healthy_account(config)

    # START-TIME ENGINE SELECTION (operator answer Q2: start time only).
    # Runs BEFORE the session overrides so that a refusal costs nothing
    # already mutated, and before the spawn gate / a2a port / forced stop
    # so an unhonourable engine refuses while the OLD agent is still up.
    # RAISES rather than degrading: an unknown --engine key does not fall
    # back to the default, and an engine that cannot be honoured does not
    # fall back to another engine (answer Q3). A legacy single-backend
    # spec with no --engine returns None here and changes nothing.
    select_engine_at_start(
        config, engine_override, probe=probe_engine
    )

    if session_override:
        config.claude.session = session_override
    if resume_id_override is not None:
        config.claude.resume_id = resume_id_override
    if one_shot and not (config.startup_prompts or config.startup_commands):
        raise RuntimeError(
            f"--one-shot requires spec.startup_prompts (or legacy "
            f"startup_commands) on agent '{config.name}'; nothing to run."
        )

    # Spawn-permission gate + lineage record (ADR-0010 Rule B / Phase 2:
    # "起動経路 = 記録経路 = ACL経路" collapsed to one path). EVERY spawn
    # path funnels through core ``agent_start`` — the MCP ``agent_start``
    # tool and the plain ``sac agents start`` CLI both reach here, not
    # just the ``sac listen`` ``POST /agents`` handler. Enforcing the
    # gate here (rather than only in the server handler) means an
    # agent-from-agent spawn is ACL-gated WITHOUT requiring a running
    # ``sac listen`` daemon — clew on Spartan can spawn capsule children
    # with no extra process. The caller identity is the parent agent's
    # ``SAC_NAME`` env (``None`` → admin / operator / lead → always
    # allowed). On allow with a real caller, the ``caller → child`` edge
    # is written to the ``lineage`` table — the same identity that
    # ``record_local_instance`` records as ``instances.spawned_by``, so
    # the two are no longer split-brained. A denied spawn raises
    # ``SpawnDeniedError`` HERE, before the runtime is built or touched.
    # The server handler still passes its request ``caller`` verbatim and
    # records lineage itself; its subprocess inherits no ``SAC_NAME`` on
    # the bare host, so this gate sees ``caller=None`` (admin) and does
    # not double-record — and ``record_lineage`` is idempotent regardless.
    enforce_spawn_gate(config.name)

    # Phase-3 (ADR-0010 Step 2) — publish the loaded spec's per-spec
    # ACL policy into ``node_comms_policy`` so check_send_acl /
    # check_spawn / derive_group see the current outbound, inbound,
    # group=solitary, and may_spawn rules on the next request. The
    # upsert is idempotent and re-runs on every start so a spec edit
    # becomes live without manual state.db surgery. Defaults preserve
    # pre-Phase-3 behaviour, so an existing YAML with no spec.comms /
    # spec.lineage blocks writes the all-allow / may_spawn=True row.
    #
    # NOT ON A DRY RUN. This publish was a write to the host's own
    # state.db, where a dry run doing it was untidy but contained. The
    # policy now lives in ONE store shared by the whole fleet, so the same
    # line makes `sac agents start --dry-run` mutate fleet-wide ACL state
    # for an agent it is not starting -- and makes the dry run fail
    # outright wherever that store is unreachable, which is how CI found
    # it: the smoke test drives the real CLI against the isolation DSN and
    # got `exit=1 ... Cannot connect to Postgres store 'node_comms_policy'`.
    # A dry run answers "would this start?" and must not write to do it.
    if not dry_run:
        persist_acl_policy(config)

    # Resolve spec.a2a.port BEFORE the runtime builds argv. ``"auto"``
    # gets a fresh allocator claim; an explicit int is recorded so
    # ``sac listen`` can find the port via state.db without re-parsing
    # the spec.yaml.
    resolve_a2a_port(config)

    # Bug #41 preflight — refuse to start when spec.claude.channels
    # requests ``server:claude-code-telegrammer`` but spec.a2a.port is
    # unset/null. Without the /v1/turn endpoint the standalone
    # telegrammer poller has no URL to POST inbound Telegram to and an
    # idle agent silently won't wake. Catching this here makes the
    # misconfig loud at ``sac agents start`` time rather than the
    # operator discovering it via "agent doesn't reply to Telegram"
    # three messages later. See ``runtimes/_sdk_channels.
    # validate_telegrammer_wake_wiring`` for the contract; F3 (MCP key
    # mis-keyed in to_home/.mcp.json) is covered by the matching
    # runner-side WARN/ERROR logs in ``_wire_telegrammer_wake``.
    from ..runtimes._sdk_channels import validate_telegrammer_wake_wiring

    validate_telegrammer_wake_wiring(
        getattr(config.claude, "channels", None),
        getattr(config.a2a, "port", None),
        agent_name=config.name,
    )

