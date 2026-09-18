"""``agent_status`` / ``agent_logs`` — read-side lifecycle queries.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports both names.
"""

from __future__ import annotations

import time
import traceback
from typing import Any, Callable, Optional

from .._state.registry import Registry
from ..config import AgentConfig, load_config
from ._runtime_select import _fallback_workdir, _get_runtime


def _resolve_account(config: AgentConfig | None) -> str:
    """Resolve stored Claude Code credential inventory for Anthropic only.

    Hermes and Codex specs retain legacy ``claude`` fields, but those harnesses
    do not authenticate through Claude Code OAuth. They return ``"unknown"``
    rather than publishing unrelated account metadata.

    Tolerant: a missing config or any resolver hiccup maps to
    ``"unknown"`` so status never fails on account lookup.
    """
    if (
        config is None
        or str(getattr(config, "harness", "") or "").strip().lower()
        != "anthropic"
    ):
        return "unknown"
    # stx-allow: fallback (reason: status output must never crash on an
    # account-resolution hiccup; ``"unknown"`` is the right degraded UX.)
    try:
        from .._account.agent_account import resolve_agent_account_label

        env = config.env if config is not None else None
        assigned = (
            getattr(getattr(config, "claude", None), "account", "") or None
            if config is not None
            else None
        )
        return resolve_agent_account_label(env, assigned_account=assigned)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return "unknown"


def _runtime_identity(
    name: str,
    config: AgentConfig | None,
    running: bool,
    *,
    registry_entry: dict | None = None,
    active_instances: list[dict] | None = None,
    local_host: str | None = None,
    birth_reader=None,
    evidence_reader=None,
) -> dict:
    """Runtime selection/auth facts, preferring this incarnation's birth."""
    birth = None
    if running:
        try:  # stx-allow: fallback (unavailable birth -> labelled spec fallback)
            from .._state.state_store import _resolve_host, list_active_instances
            from ._runtime_identity import resolve_bound_birth_records

            host = local_host or _resolve_host(None)
            snapshot = (
                active_instances
                if active_instances is not None
                else list_active_instances(host=None)
            )
            births = resolve_bound_birth_records(
                [registry_entry or {"name": name}],
                active_instances=snapshot,
                local_host=host,
                evidence_reader=evidence_reader,
                birth_reader=birth_reader,
            )
            birth = births.get(name)
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            birth = None
    from ._runtime_identity import resolve_runtime_identity

    return resolve_runtime_identity(config, running=running, birth_record=birth)


def _remote_instance_status(name: str) -> dict | None:
    """Build a status dict from the active ``instances`` row for ``name``.

    Used when the LOCAL file registry has no entry — the case for a
    cross-host-dispatched agent, whose row was written into the
    ``instances`` table by the dispatcher (``remote=1`` + peer ``host``
    + peer-resolved ``bound_port``). Returns ``None`` when no active row
    exists (caller raises the normal "not found" error), or on any
    lookup failure.

    The shape mirrors the canonical ``agent_status`` keys callers
    expect, surfacing the family-tree fields (``host``, ``a2a_port`` /
    ``bound_port``, ``remote``, ``spawned_by``) so a remote agent
    resolves rather than erroring.
    """
    try:
        from .._state.state_store import list_active_instances

        rows = [r for r in list_active_instances() if r.get("name") == name]
        if not rows:
            return None
        # list_active_instances orders started_at DESC → newest first.
        row = rows[0]
        bound = row.get("bound_port")
        if bound is None:
            bound = row.get("a2a_port")
        result = {
            "name": name,
            "config": "",
            "screen": row.get("screen", "") or "",
            "started_at": row.get("started_at", "") or "",
            # The instances row says the agent is active (ended_at IS
            # NULL); reaching the remote runtime to confirm is the
            # cross-host dispatcher's job, not this read-side resolver.
            "status": "running",
            "model": "unknown",
            "runtime": "unknown",
            "harness": "unknown",
            "engine": "unknown",
            "billing_mode": "unspecified",
            "auth_identity": "unknown",
            "runtime_identity_source": "unknown",
            # Cross-host agent: its credentials live on the remote host,
            # not resolvable from here. Keep compatibility alias explicit.
            "stored_credential": "unknown",
            "account": "unknown",
            "host": row.get("host", "") or "",
            "a2a_port": row.get("a2a_port"),
            "bound_port": bound,
            "remote": bool(row.get("remote")),
            "spawned_by": row.get("spawned_by"),
        }
        from .._state.state_store import latest_authoritative_heartbeats

        beat = next(
            (
                value
                for value in latest_authoritative_heartbeats()
                if value.get("agent_id") == name
            ),
            None,
        )
        if beat is not None:
            from .._state.authoritative_heartbeat import classify_resident_state

            process_evidence = beat.get("_process_alive")
            process_alive = (
                process_evidence if isinstance(process_evidence, bool) else None
            )
            result.update(
                {
                    "model": beat.get("model") or "unknown",
                    "runtime": beat.get("runtime") or "unknown",
                    "harness": beat.get("harness") or "unknown",
                    "engine": beat.get("engine") or "unknown",
                    "host": beat.get("host") or result["host"],
                    "heartbeat": beat,
                    "resident_state": classify_resident_state(
                        beat,
                        now=time.time(),
                        process_alive=process_alive,
                        federation_connected=bool(
                            beat.get("_federation_connected")
                        ),
                        progress_stale_s=120.0,
                    ),
                }
            )
        from .._state.observation import DefinitionState, build_agent_observation

        result["liveness"] = {
            "verdict": "unknown",
            "evidence": [
                {
                    "source": "registry",
                    "verdict": "unknown",
                    "detail": "remote active row is not a live process observation",
                }
            ],
        }
        result["observation"] = build_agent_observation(
            result, definition_state=DefinitionState.MISSING
        )
        return result
    except Exception:  # stx-allow: fallback (reason: best-effort cross-host status — caller raises the normal "not found" error when None)
        return None


def _heartbeat_only_status(name: str) -> dict | None:
    """Resolve a fleet-visible resident from its current host lease alone."""
    try:
        from .._state.authoritative_heartbeat import classify_resident_state
        from .._state.state_store import latest_authoritative_heartbeats

        beat = next(
            (
                value
                for value in latest_authoritative_heartbeats()
                if value.get("agent_id") == name
            ),
            None,
        )
        if beat is None:
            return None
        process_evidence = beat.get("_process_alive")
        process_alive = (
            process_evidence if isinstance(process_evidence, bool) else None
        )
        resident_state = classify_resident_state(
            beat,
            now=time.time(),
            process_alive=process_alive,
            federation_connected=bool(beat.get("_federation_connected")),
            progress_stale_s=120.0,
        )
        running = resident_state in {"idle", "active", "blocked", "stalled"}
        return {
            "name": name,
            "config": "",
            "screen": "",
            "started_at": "",
            "status": "running" if running else "stopped",
            "model": beat.get("model") or "unknown",
            "runtime": beat.get("runtime") or "unknown",
            "harness": beat.get("harness") or "unknown",
            "engine": beat.get("engine") or "unknown",
            "billing_mode": "unspecified",
            "auth_identity": "unknown",
            "runtime_identity_source": "authoritative-heartbeat",
            "stored_credential": "unknown",
            "account": "unknown",
            "host": beat.get("host") or "",
            "resident_state": resident_state,
            "heartbeat": beat,
            "liveness": {
                "verdict": "alive" if running else "unknown",
                "evidence": [
                    {
                        "source": "authoritative-heartbeat",
                        "verdict": resident_state,
                        "detail": "host lease and resident progress projection",
                    }
                ],
            },
        }
    except Exception:  # stx-allow: fallback (unavailable fleet lease is UNKNOWN and caller retains the normal not-found verdict)
        return None


def _liveness_block(
    name: str,
    config: AgentConfig | None,
    runtime_factory: Optional[Callable[[AgentConfig], Any]],
) -> dict:
    """The ternary liveness verdict + its evidence, for ``agent_status``.

    Tolerant by construction: any failure to gather degrades to an UNKNOWN
    verdict with the reason attached — never to a fabricated DEAD, and never to
    an exception that takes the whole status command down with it.
    """
    from ._verdict import (
        INSTRUMENT_NO_OBSERVATION,
        SOURCE_RESOLVER,
        UNKNOWN,
        LivenessVerdict,
        Signal,
    )
    from ._verdict_resolve import resolve_verdict

    try:
        runtime = None
        if config is not None:
            factory = runtime_factory or _get_runtime
            runtime = factory(config)
        return resolve_verdict(name, config, runtime).to_dict()
    except Exception as exc:  # stx-allow: fallback (reason: an un-gatherable verdict is UNKNOWN with its reason — never a fabricated DEAD, and never a crashed status command)
        return LivenessVerdict(
            agent=name,
            verdict=UNKNOWN,
            signals=(
                Signal(
                    SOURCE_RESOLVER,
                    UNKNOWN,
                    f"could not gather liveness evidence ({type(exc).__name__}: {exc})",
                    INSTRUMENT_NO_OBSERVATION,
                ),
            ),
        ).to_dict()


def agent_status(
    name: str,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
) -> dict:
    """Get detailed status for an agent.

    Args:
        name: Agent name.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        # Cross-host fallback (sac-agent-spawn design, Rule B/F): a
        # remote-dispatched agent has no LOCAL file-registry entry — its
        # row lives in the ``instances`` table written by the cross-host
        # dispatcher. Resolve status from there so ``sac agents status
        # <remote>`` reports host + bound_port + remote + spawned_by
        # instead of raising "not found in registry".
        remote_status = _remote_instance_status(name)
        if remote_status is not None:
            return remote_status
        heartbeat_status = _heartbeat_only_status(name)
        if heartbeat_status is not None:
            return heartbeat_status
        raise RuntimeError(f"Agent '{name}' not found in registry")

    runtime_factory = runtime_factory or _get_runtime
    # stx-allow: fallback (reason: YAML or runtime may be unavailable; status should degrade to stopped=False rather than raise)
    try:
        config = load_config(entry["config"])
        runtime = runtime_factory(config)
        running = runtime.is_running(config)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        traceback.print_exc()
        running = False
        config = None

    result = {
        "name": name,
        "config": entry.get("config", ""),
        "screen": entry.get("screen", ""),
        "started_at": entry.get("started_at", ""),
        "status": "running" if running else "stopped",
        # Stored credential inventory is not actual runtime auth identity.
        "stored_credential": _resolve_account(config),
    }
    result["account"] = result["stored_credential"]  # deprecated inventory alias

    # Runtime-specific detection stays behind a neutral control-plane shape.
    # A live process can still be unable to admit turns (for example a
    # circuit-breaker latch), which is distinct from liveness and idle state.
    if config is not None:
        try:
            control = runtime.control_state(config)
        except Exception:  # stx-allow: fallback (reason: optional adapter observation must not break status)
            control = None
        if control is not None:
            result["runtime_control"] = control

    # TERNARY liveness verdict + THE EVIDENCE FOR IT.
    #
    # ``status`` above is the legacy BOOL ("running"/"stopped"), and it cannot
    # say "I don't know" — so it says one of the two poles and the reader has no
    # way to tell a confident verdict from a coin-flip. An operator staring at
    # ``running | pid=None`` learns nothing at all.
    #
    # ``liveness`` says WHICH and WHY: "ALIVE (delivery: 1 live inbox
    # subscriber)" / "UNKNOWN (heartbeat: beat is 5086s stale …; registry: …)".
    # Positive ALIVE evidence is nevertheless allowed to repair the legacy
    # projection.  A config that became invalid after an agent started makes
    # the runtime-specific boolean probe unavailable, but it does not stop the
    # already-running process.  Returning ``status=stopped`` beside a fresh
    # heartbeat saying ALIVE hid that process from ``--all-running``.  This is
    # monotonic: UNKNOWN/DEAD never manufactures ``running`` and the complete
    # evidence remains available beside the compatibility field.
    liveness = _liveness_block(name, config, runtime_factory)
    result["liveness"] = liveness
    if liveness.get("verdict") == "alive":
        result["status"] = "running"
    result.update(
        _runtime_identity(
            name,
            config,
            result["status"] == "running",
            registry_entry=entry,
        )
    )
    # ``config.remote`` was deleted in WI-6; spec.host (host pinning)
    # is the v3 equivalent and is recorded in state.db's ``instances``
    # table rather than echoed back through ``status``.

    # Hook-points / listen / extensions plumbing (todo#286 Phase 4).
    # Counts are exposed so consumers can see what's wired up; command
    # bodies are intentionally NOT echoed to avoid leaking URLs or
    # secrets through status --json.
    if config is not None:
        hooks = config.hooks or {}
        result["hooks_configured"] = {
            key: len(hooks.get(key, []) or [])
            for key in (
                "pre_start",
                "post_start",
                "pre_stop",
                "post_stop",
                "on_compact",
                "on_restart",
                "on_diff",
            )
        }
        result["listen"] = [
            {
                "port": lp.port,
                "proto": lp.proto,
                "path": lp.path,
                "name": lp.name,
                "owner": lp.owner,
            }
            for lp in (config.listen or [])
        ]
        # Opaque pass-through — echoed verbatim.
        result["extensions"] = dict(config.extensions or {})
    else:
        result["hooks_configured"] = {}
        result["listen"] = []
        result["extensions"] = {}

    # Snapshot block — cheap read from cache (todo#286). Never re-gathers.
    # stx-allow: fallback (reason: snapshot module may not yet exist or cache may be absent on first run; None snapshot is valid initial state)
    try:
        from .._state.snapshot import read_latest

        latest = read_latest(name)
        if latest is not None:
            result["snapshot"] = {
                "timestamp": latest.get("timestamp"),
                "has_diff": latest.get("has_diff", False),
                "diff_fields": latest.get("diff_fields", []),
            }
        else:
            result["snapshot"] = None
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        result["snapshot"] = None

    # Enrich with the rich metadata payload. Canonical source for the
    # Agents-tab dashboard; the MCP sidecar heartbeat shells out to this
    # command rather than duplicating the logic in TypeScript.
    # stx-allow: fallback (reason: agent_meta requires psutil and an active tmux session; metadata enrichment is optional and must never break status)
    try:
        from .._state.agent_meta import collect_rich

        workdir = config.expanded_workdir if config else _fallback_workdir(name)
        session = entry.get("screen", "") or (config.screen_name if config else name)
        rich = collect_rich(name=name, workdir=workdir, session=session)
        # Prefer transcript-derived started_at only if the registry
        # doesn't have one.
        if not result.get("started_at") and rich.get("started_at_transcript"):
            result["started_at"] = rich["started_at_transcript"]
        rich.pop("started_at_transcript", None)
        rich.pop("model_transcript", None)
        # Never let rich overwrite the canonical registry/config fields.
        for k, v in rich.items():
            result.setdefault(k, v)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        # Never let metadata collection break status.
        pass

    # Operator mandate (lead a2a 1781e82a, 2026-06-14): surface
    # ``session_jsonl_bytes`` / ``session_jsonl_last_write`` /
    # ``heartbeat_at`` at the TOP level of the status JSON so the
    # kick-cycle can read MOVEMENT objectively without scraping
    # ``heartbeat.json`` or walking the SDK ``sdk_session`` block.
    # All three keys are always present; missing-data renders as
    # ``0`` / ``""`` (explicit empty values, NOT null) so consumers
    # never need a key-existence check.
    # stx-allow: fallback (reason: a state-dir read failure should never break
    # the status command — degrade to the explicit empty shape)
    state_dir = None
    try:
        from ._session_movement import resolve_state_dir, status_movement_fields

        state_dir = resolve_state_dir(name)
        movement = status_movement_fields(state_dir)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        movement = {
            "session_jsonl_bytes": 0,
            "session_jsonl_last_write": "",
            "heartbeat_at": "",
        }
    for k, v in movement.items():
        # Don't overwrite a field that a prior enrich step already set —
        # the additive contract says NEW keys, not "always replaces".
        result.setdefault(k, v)

    try:
        from .._runners._session_state import read_heartbeat

        local_heartbeat = read_heartbeat(state_dir) if state_dir is not None else None
        if isinstance(local_heartbeat, dict) and isinstance(
            local_heartbeat.get("authoritative_heartbeat"), dict
        ):
            from .._state.authoritative_heartbeat import classify_resident_state

            resident = dict(local_heartbeat["authoritative_heartbeat"])
            result["heartbeat"] = resident
            result["resident_state"] = classify_resident_state(
                resident,
                now=time.time(),
                process_alive=result.get("status") == "running",
                federation_connected=(
                    float(resident.get("lease_expires_at") or 0) >= time.time()
                ),
                progress_stale_s=120.0,
            )
    except Exception:  # stx-allow: fallback (reason: heartbeat enrichment is optional and must not break status)
        pass

    from .._state.observation import DefinitionState, build_agent_observation

    result["observation"] = build_agent_observation(
        result,
        definition_state=(
            DefinitionState.VALID if config is not None else DefinitionState.INVALID
        ),
    )

    return result


def agent_logs(
    name: str,
    lines: int = 50,
    registry: Registry | None = None,
    *,
    runtime_factory: Optional[Callable[[AgentConfig], Any]] = None,
) -> str:
    """Get recent logs from an agent.

    Args:
        name: Agent name.
        lines: Number of trailing log lines to return.
        registry: Optional registry instance.
        runtime_factory: Real runtime factory (default :func:`_get_runtime`).
    """
    registry = registry or Registry()
    entry = registry.get(name)
    if entry is None:
        raise RuntimeError(f"Agent '{name}' not found in registry")

    runtime_factory = runtime_factory or _get_runtime
    config = load_config(entry["config"])
    runtime = runtime_factory(config)
    return runtime.logs(config, lines)
