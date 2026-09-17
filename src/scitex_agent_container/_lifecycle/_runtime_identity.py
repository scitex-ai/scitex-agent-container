"""Resolve the runtime identity shown by agent-list consumers.

Running rows prefer the immutable, redacted birth certificate.  A current
``spec.yaml`` is only a labelled fallback: it can change after launch and an
explicit ``--engine`` selection never edits it.  The returned shape is safe for
human/JSON/browser surfaces and contains source names only, never credentials.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

IDENTITY_FIELDS = (
    "runtime",
    "harness",
    "engine",
    "model",
    "billing_mode",
    "auth_identity",
    "runtime_identity_source",
)


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _config_snapshot(config: Any) -> Mapping[str, Any] | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        return dataclasses.asdict(config)
    return {
        "runtime": getattr(config, "runtime", ""),
        "harness": getattr(config, "harness", ""),
        "engine_key": getattr(config, "engine_key", ""),
        "model": getattr(config, "model", ""),
        "subscription_provider": getattr(config, "subscription_provider", ""),
        "subscription_account": getattr(config, "subscription_account", ""),
        "claude": getattr(config, "claude", None),
    }


def _birth_snapshot(record: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    payload = record.get("compiled_spec_json")
    if isinstance(payload, Mapping):
        return payload
    if not isinstance(payload, str) or not payload.strip():
        return None
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _subscription_identity(snapshot: Mapping[str, Any]) -> tuple[str, str] | None:
    provider = _text(snapshot.get("subscription_provider"))
    account = _text(snapshot.get("subscription_account"))
    if not provider or not account:
        return None
    prefix, separator, unqualified = account.partition(":")
    if separator and prefix == provider and unqualified:
        account = unqualified
    return provider, account


def _provider(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    claude = _mapping(snapshot.get("claude"))
    provider = claude.get("provider")
    if not isinstance(provider, Mapping):
        provider = snapshot.get("provider")
    return _mapping(provider)


_CLAUDE_HARNESSES = frozenset({"anthropic", "claude", "claude-code"})
_SAFE_ACCOUNT_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")


def _is_claude_harness(snapshot: Mapping[str, Any]) -> bool:
    """Whether legacy ``claude.*`` auth fields belong to this harness.

    The compiled config retains a legacy ``claude`` block for every harness.
    It is executable authentication configuration only for Claude Code.  Treating
    it as Hermes/Codex identity leaks unrelated credential inventory and falsely
    attributes the running process to an account it never opened.
    """
    return _text(snapshot.get("harness")).lower() in _CLAUDE_HARNESSES


def _trusted_account_label(value: Any) -> str:
    """Return a safe persisted launch label, never a path-derived guess."""
    label = _text(value)
    return label if _SAFE_ACCOUNT_LABEL.fullmatch(label) else ""


def _billing_mode(snapshot: Mapping[str, Any]) -> str:
    # Only an explicit declaration may say "usage".  An API-key source is an
    # authentication fact, not a billing contract, so it deliberately does not
    # affect this value.
    explicit = _text(snapshot.get("billing_mode") or snapshot.get("billing")).lower()
    if explicit in {"subscription", "usage", "unspecified"}:
        return explicit
    if _subscription_identity(snapshot) is not None:
        return "subscription"
    return "unspecified"


def _auth_identity(snapshot: Mapping[str, Any]) -> str:
    subscription = _subscription_identity(snapshot)
    if subscription is not None:
        return f"{subscription[0]}/{subscription[1]}"
    auth_env = _text(_provider(snapshot).get("auth_token_env"))
    if auth_env:
        # Public identity is deliberately opaque. Environment-variable names
        # routinely encode customer, tenant, deployment and source metadata;
        # even a shortened prefix/suffix discloses that private inventory.
        return "api-key"
    if not _is_claude_harness(snapshot):
        return "unknown"
    claude = _mapping(snapshot.get("claude"))
    account = _trusted_account_label(claude.get("account"))
    if account:
        return f"claude-code:{account}"
    return "unknown"


def bind_active_instance(
    name: str,
    active_instances: list[dict],
    *,
    local_host: str,
    marker_id: str | None,
    pid: int | None,
    session: str | None,
    heartbeat: Mapping[str, Any] | None,
) -> dict | None:
    """Bind a live local runtime to exactly one active instance row.

    Name+host is only a population filter, never authority: duplicate active
    rows are possible after crashes and store races.  Authority requires a
    process-owned incarnation marker/heartbeat, or an exact PID/session handle.
    Conflicting evidence abstains rather than choosing the newest row.
    """
    candidates = [
        row
        for row in active_instances
        if str(row.get("name") or "") == name
        and str(row.get("host") or "") == local_host
        and not bool(row.get("remote"))
    ]
    if not candidates:
        return None

    ids = {
        value
        for value in (
            _text(marker_id),
            _text(_mapping(heartbeat).get("incarnation_id")),
        )
        if value
    }
    if len(ids) > 1:
        return None
    if ids:
        wanted = next(iter(ids))
        matches = [row for row in candidates if _text(row.get("id")) == wanted]
        return matches[0] if len(matches) == 1 else None

    evidence_matches: list[set[int]] = []
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        evidence_matches.append(
            {idx for idx, row in enumerate(candidates) if row.get("pid") == pid}
        )
    session_text = _text(session)
    if session_text:
        evidence_matches.append(
            {
                idx
                for idx, row in enumerate(candidates)
                if _text(row.get("screen")) == session_text
            }
        )
    if not evidence_matches or any(not matches for matches in evidence_matches):
        return None
    common = set.intersection(*evidence_matches)
    return candidates[next(iter(common))] if len(common) == 1 else None


def local_binding_evidence(name: str, registry_row: Mapping[str, Any]) -> dict[str, Any]:
    """Read process-owned incarnation evidence without consulting the store."""
    marker_id = None
    heartbeat = None
    try:
        from .._runners._session_state import read_heartbeat, read_instance_id
        from ._session_movement import resolve_state_dir

        state_dir = resolve_state_dir(name)
        if state_dir is not None:
            marker_id = read_instance_id(state_dir)
            heartbeat = read_heartbeat(state_dir)
    except Exception:  # stx-allow: fallback (missing marker means abstain/try exact process handles)
        pass
    return {
        "marker_id": marker_id,
        # Registry.pid is the short-lived launcher PID (Registry.add defaults to
        # os.getpid), not the runtime process.  Only an explicitly published
        # runtime handle may participate in PID binding.
        "pid": registry_row.get("runtime_pid") or registry_row.get("agent_pid"),
        "session": registry_row.get("screen"),
        "heartbeat": heartbeat,
    }


def resolve_bound_birth_records(
    registry_rows: list[dict],
    *,
    active_instances: list[dict],
    local_host: str,
    evidence_reader: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
    birth_reader: Callable[[tuple[str, ...]], dict[str, dict]] | None = None,
) -> dict[str, dict]:
    """Resolve launch records with one bounded birth read for exact bindings."""
    evidence_fn = evidence_reader or local_binding_evidence
    bound_ids: dict[str, str] = {}
    for registry_row in registry_rows:
        name = _text(registry_row.get("name"))
        if not name:
            continue
        evidence = evidence_fn(name, registry_row)
        bound = bind_active_instance(
            name,
            active_instances,
            local_host=local_host,
            marker_id=_text(evidence.get("marker_id")) or None,
            pid=evidence.get("pid"),
            session=_text(evidence.get("session")) or None,
            heartbeat=_mapping(evidence.get("heartbeat")),
        )
        if bound is not None and _text(bound.get("id")):
            bound_ids[name] = _text(bound["id"])
    wanted = tuple(dict.fromkeys(bound_ids.values()))
    if not wanted:
        return {}
    if birth_reader is None:
        from .._state.state_store_incarnations import get_incarnations

        birth_reader = get_incarnations
    births = birth_reader(wanted)
    return {
        name: births[instance_id]
        for name, instance_id in bound_ids.items()
        if instance_id in births
    }


def _resolved(snapshot: Mapping[str, Any] | None, source: str) -> dict[str, str]:
    if snapshot is None:
        return {
            "runtime": "unknown",
            "harness": "unknown",
            "engine": "unknown",
            "model": "unknown",
            "billing_mode": "unspecified",
            "auth_identity": "unknown",
            "runtime_identity_source": "unknown",
        }
    return {
        "runtime": _text(snapshot.get("runtime")) or "unknown",
        "harness": _text(snapshot.get("harness")) or "unknown",
        "engine": _text(snapshot.get("engine_key") or snapshot.get("engine"))
        or "unknown",
        "model": _text(snapshot.get("model")) or "unknown",
        "billing_mode": _billing_mode(snapshot),
        "auth_identity": _auth_identity(snapshot),
        "runtime_identity_source": source,
    }


def resolve_runtime_identity(
    config: Any,
    *,
    running: bool,
    birth_record: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return honest Billing/Auth/Harness/Engine/Model fields and provenance."""
    if running:
        birth = _birth_snapshot(birth_record)
        if birth is not None:
            return _resolved(birth, "birth_certificate")
    spec = _config_snapshot(config)
    return _resolved(spec, "spec" if spec is not None else "unknown")


__all__ = [
    "IDENTITY_FIELDS",
    "bind_active_instance",
    "local_binding_evidence",
    "resolve_bound_birth_records",
    "resolve_runtime_identity",
]
