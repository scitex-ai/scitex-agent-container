"""Lead-state-handover (ZOO#12) — sac runtime helpers.

Reads ZOO#12-specific keys from the agent YAML spec without polluting the
canonical :class:`AgentConfig` schema:

  - ``cardinality_enforced_at_hub: true``  (FR-C — sent to the hub on WS
    accept; the hub enforces 4001 on duplicate-name-different-UUID)
  - ``priority_list: [host_a, host_b, ...]`` (FR-B — order of preferred
    owner hosts; a healthier higher-priority host triggers failback)

Plus three lifecycle pieces:

  - ``ensure_instance_uuid(config)`` — generates ``uuid4()`` once per
    agent_start invocation and writes it to ``config.env`` so the
    runtime's ``_build_env_exports`` propagates it as
    ``SCITEX_AGENT_INSTANCE_UUID`` (FR-E)
  - ``hydrate_from_hub(config)``    — pulls the latest snapshot from the
    hub and writes its payload to ``<workdir>/.scitex/handover/snapshot.json``
    so the agent's boot-time skill can pick it up (FR-A)
  - ``start_failback_poller(config)`` — daemon thread that polls
    ``/api/agents/<name>/owner/`` every 60 s; on a healthier
    higher-priority host showing up, pushes a snapshot then exits the
    process so the local lead steps aside (FR-B)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import uuid
from pathlib import Path
from typing import Any

import yaml

from .. import hub_client

logger = logging.getLogger(__name__)

_FAILBACK_POLL_INTERVAL_S = 60.0


# ---------- spec readers --------------------------------------------------


def _load_raw_spec(config_path: str) -> dict:
    """Load the YAML at ``config_path`` and return the inner spec dict.

    The orochi spec format wraps everything under ``spec:``; older specs
    are flat. Tolerate both.
    """
    if not config_path:
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("_handover: cannot read spec %s: %s", config_path, exc)
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("spec"), dict):
        return raw["spec"]
    return raw if isinstance(raw, dict) else {}


def read_cardinality_enforced(config_path: str) -> bool:
    spec = _load_raw_spec(config_path)
    return bool(spec.get("cardinality_enforced_at_hub", False))


def read_priority_list(config_path: str) -> list[str]:
    spec = _load_raw_spec(config_path)
    raw = spec.get("priority_list") or []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


# ---------- FR-E identity -------------------------------------------------


def ensure_instance_uuid(config) -> str:
    """Generate a UUID for this start and write it into ``config.env``.

    Idempotent on the AgentConfig instance: if ``config.env`` already
    has ``SCITEX_AGENT_INSTANCE_UUID`` (e.g. caller set it explicitly),
    leave it alone.
    """
    env = getattr(config, "env", None)
    env = env if isinstance(env, dict) else {}
    existing = env.get("SCITEX_AGENT_INSTANCE_UUID", "")
    if existing:
        return str(existing)
    new_uuid = str(uuid.uuid4())
    env["SCITEX_AGENT_INSTANCE_UUID"] = new_uuid
    config.env = env
    return new_uuid


# ---------- FR-A snapshot hydrate ----------------------------------------


def _handover_dir(config) -> Path:
    workdir = Path(config.expanded_workdir).expanduser()
    out = workdir / ".scitex" / "handover"
    out.mkdir(parents=True, exist_ok=True)
    return out


def hydrate_from_hub(config) -> bool:
    """Fetch the latest snapshot from the hub and dump it locally.

    Best-effort: returns False on any error (no token / 404 / network).
    The snapshot is written atomically as
    ``<workdir>/.scitex/handover/snapshot.json``; the agent's boot-time
    skill consumes it (or ignores it on first start).
    """
    snap = hub_client.fetch_snapshot(config.name)
    if not snap or "payload" not in snap:
        return False
    out_dir = _handover_dir(config)
    target = out_dir / "snapshot.json"
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        tmp.replace(target)
        logger.info(
            "hydrate_from_hub: %s wrote %d bytes from owner=%s",
            config.name,
            len(snap.get("payload") or {}),
            snap.get("owner_host", ""),
        )
        return True
    except OSError as exc:
        logger.warning("hydrate_from_hub: write failed: %s", exc)
        return False


def push_pre_stop_snapshot(config, payload: dict[str, Any] | None = None) -> bool:
    """Push a snapshot to the hub right before the agent stops.

    The default payload is a sentinel ``{"reason": "pre_stop"}``; richer
    state (e.g. transcript tail, memory) is the agent's responsibility
    via its own pre_stop hook.
    """
    body = payload if payload is not None else {"reason": "pre_stop"}
    owner = os.environ.get("SCITEX_OROCHI_MACHINE", "") or os.environ.get(
        "SCITEX_AGENT_CONTAINER_HOSTNAME", ""
    )
    return hub_client.push_snapshot(config.name, body, owner_host=owner)


# ---------- FR-B priority-failback poller --------------------------------


_pollers: dict[str, threading.Thread] = {}
_pollers_lock = threading.Lock()


def _self_host() -> str:
    return os.environ.get("SCITEX_OROCHI_MACHINE", "") or os.environ.get(
        "SCITEX_AGENT_CONTAINER_HOSTNAME", ""
    )


def _should_step_aside(self_host: str, owner_payload: dict) -> bool:
    """Return True iff a higher-priority host than us is healthy.

    The hub's ``healthy`` map only flags hosts that have an active
    online registration; a higher-priority host that's not in healthy
    is by definition not a contender.
    """
    if not self_host:
        return False
    priority = owner_payload.get("priority_list") or []
    healthy = owner_payload.get("healthy") or {}
    if self_host not in priority:
        return False
    self_idx = priority.index(self_host)
    for i, host in enumerate(priority[:self_idx]):
        if healthy.get(host):
            logger.info(
                "_should_step_aside: higher-priority host %s healthy (idx=%d, self=%s idx=%d)",
                host,
                i,
                self_host,
                self_idx,
            )
            return True
    return False


def _failback_loop(config, stop_event: threading.Event) -> None:
    while not stop_event.wait(_FAILBACK_POLL_INTERVAL_S):
        try:
            owner = hub_client.fetch_owner(config.name)
            if _should_step_aside(_self_host(), owner):
                logger.warning(
                    "failback_poller: stepping aside for higher-priority owner of %s",
                    config.name,
                )
                push_pre_stop_snapshot(config, {"reason": "priority_failback"})
                # SIGTERM lets the agent's own pre_stop hooks fire via
                # the runtime's normal shutdown path.
                os.kill(os.getpid(), signal.SIGTERM)
                return
        except Exception:
            logger.exception("failback_poller: tick failed for %s", config.name)


def start_failback_poller(config) -> threading.Event | None:
    """Spawn a daemon thread that polls /owner/ for this agent.

    No-op (returns ``None``) if no priority_list is configured for this
    agent — without one there's no defined "higher priority" host.
    Returns the stop event so callers can shut the poller down (the
    process exit also kills the daemon thread).
    """
    if not read_priority_list(config.config_path):
        return None
    name = config.name
    with _pollers_lock:
        if name in _pollers and _pollers[name].is_alive():
            logger.debug("start_failback_poller: %s already running", name)
            return None
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_failback_loop,
            args=(config, stop_event),
            name=f"sac-failback-{name}",
            daemon=True,
        )
        _pollers[name] = thread
        thread.start()
    return stop_event
