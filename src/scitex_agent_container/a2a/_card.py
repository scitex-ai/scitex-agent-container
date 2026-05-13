"""A2A AgentCard projection from a v3 sac YAML.

**Canonical source for v3 → AgentCard projection.** The scitex-cloud
mirror at ``apps/infra/a2a_app/_card.py`` (orochi public surface) is
expected to layer ``x-orochi`` enrichment on top of the field set
this module produces. Until the projection logic is extracted into a
shared dependency, the two implementations must be kept in sync:
canonical = THIS file; mirror = scitex-cloud `_card.py`.

No fleet imports. The projection is request-aware: pass
``base_url`` so each card advertises the URL the client actually used.

sac-internal fields live under ``x-scitex-agent-container``; this is
intentionally NOT ``x-orochi`` because the orochi extension namespace
is owned by that project, not sac. A standalone ``sac a2a serve``
agent therefore won't carry an ``x-orochi`` block — that's by design.
"""

from __future__ import annotations

from typing import Any

from a2a.types import AgentCard
from google.protobuf.json_format import ParseDict

DEFAULT_INPUT_MODES = ["text/plain", "application/json"]
DEFAULT_OUTPUT_MODES = ["text/plain", "application/json"]


def _read_description(name: str, v3: dict[str, Any]) -> str:
    """Return the A2A card description from metadata.labels.description."""
    labels = (v3.get("metadata") or {}).get("labels") or {}
    desc = labels.get("description", "")
    if desc:
        return desc
    role = labels.get("role", "")
    if role:
        return f"sac agent: {name} ({role})"
    return f"sac agent: {name}"


def _scheduling(spec: dict[str, Any]) -> dict[str, Any]:
    if "hosts" in spec:
        return {"mode": "multi-instance", "hosts": spec["hosts"]}
    if "host" in spec:
        h = spec["host"]
        priority = h if isinstance(h, list) else ([h] if h else [])
        return {"mode": "singleton", "priority": priority}
    return {"mode": "singleton", "priority": []}


def project_card(name: str, v3: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Project a single agent's v3 YAML into an A2A AgentCard dict."""
    base = base_url.rstrip("/")
    metadata = v3.get("metadata") or {}
    labels = metadata.get("labels") or {}
    spec = v3.get("spec") or {}
    caps_csv = labels.get("capabilities", "") or ""
    capabilities_tags = [c.strip() for c in caps_csv.split(",") if c.strip()]
    # v3 rejected `spec.skills` (skills moved to dot_claude/skills/).
    # Operator-declared skill IDs now come via metadata.labels.skills as a
    # CSV; we still tolerate the legacy spec.skills.required for any v2
    # YAML that reaches the projector before validation strips it.
    skills_csv = labels.get("skills", "") or ""
    label_skills = [s.strip() for s in skills_csv.split(",") if s.strip()]
    legacy_skills = (spec.get("skills") or {}).get("required") or []
    required_skills = list(label_skills) + list(legacy_skills)
    role = labels.get("role", "agent")
    function = labels.get("function", "")

    return {
        "name": name,
        "description": _read_description(name, v3),
        "version": v3.get("apiVersion", "scitex-agent-container/v3"),
        "url": f"{base}/v1/sac/agents/{name}",
        "provider": {
            "organization": labels.get("team", "scitex-agent-container"),
            "url": "https://scitex.ai",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "authentication": {"schemes": ["none"]},
        "defaultInputModes": list(DEFAULT_INPUT_MODES),
        "defaultOutputModes": list(DEFAULT_OUTPUT_MODES),
        "skills": [
            {
                "id": f"{name}.{role}",
                "name": role,
                "description": (
                    function.replace(",", ", ")
                    if function
                    else f"{role} for {labels.get('team', 'sac')}"
                ),
                "tags": sorted(set(capabilities_tags + list(required_skills))),
            }
        ],
        "x-scitex-agent-container": {
            "role_class": role,
            "cardinality": labels.get("cardinality"),
            "scheduling": _scheduling(spec),
            "runtime": spec.get("runtime"),
            # v3 moves model under spec.claude.model; legacy v2 had it at
            # spec.model. Prefer v3, fall back to v2 for back-compat.
            "model": (spec.get("claude") or {}).get("model") or spec.get("model"),
            "multiplexer": spec.get("multiplexer"),
            "required_skills": list(required_skills),
        },
    }


def project_card_proto(name: str, v3: dict[str, Any], base_url: str) -> AgentCard:
    """Project a single agent's v3 YAML into the SDK's protobuf ``AgentCard``.

    SDK 1.0.x's :class:`AgentCard` is a protobuf message (not pydantic),
    and only accepts a strict subset of the dict fields we serve at
    ``/.well-known/agent.json``. This helper builds the proto card the
    SDK's :class:`DefaultRequestHandler` requires.

    sac-only extension fields (``x-scitex-agent-container``) are dropped
    from the proto — they're served as-is on the GET ``.well-known``
    routes via :func:`project_card`. ``capabilities.streaming`` is
    forced to ``True`` since sac executors enqueue task-update events
    to support ``message/stream``.
    """
    card_dict = project_card(name, v3, base_url)

    keep_keys = {"name", "description", "version"}
    minimal: dict[str, Any] = {k: card_dict[k] for k in keep_keys if k in card_dict}

    caps_in = card_dict.get("capabilities") or {}
    minimal["capabilities"] = {
        "streaming": True,
        "push_notifications": bool(caps_in.get("pushNotifications", False)),
    }

    skill_keep = {"id", "name", "description", "tags"}
    minimal["skills"] = [
        {k: skill[k] for k in skill_keep if k in skill}
        for skill in (card_dict.get("skills") or [])
    ]

    prov = card_dict.get("provider") or {}
    minimal["provider"] = {k: prov[k] for k in ("organization", "url") if k in prov}

    minimal["default_input_modes"] = list(card_dict.get("defaultInputModes") or [])
    minimal["default_output_modes"] = list(card_dict.get("defaultOutputModes") or [])

    return ParseDict(minimal, AgentCard())


def fleet_card(
    base_url: str, agents: list[str], description: str | None = None
) -> dict[str, Any]:
    """Project a fleet-level AgentCard listing ``agents``."""
    base = base_url.rstrip("/")
    return {
        "name": "scitex-agent-container",
        "description": description
        or "scitex-agent-container fleet — A2A protocol surface.",
        "version": "scitex-agent-container/1",
        "url": base,
        "provider": {
            "organization": "scitex-agent-container",
            "url": "https://scitex.ai",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "authentication": {"schemes": ["none"]},
        "defaultInputModes": list(DEFAULT_INPUT_MODES),
        "defaultOutputModes": list(DEFAULT_OUTPUT_MODES),
        "skills": [
            {
                "id": "sac.fleet",
                "name": "fleet",
                "description": ("sac-served fleet — see /v1/sac/agents/ for members."),
                "tags": ["multi-agent", "scitex-agent-container"],
            }
        ],
        "x-scitex-agent-container": {
            "agents": [{"name": n, "url": f"{base}/v1/sac/agents/{n}"} for n in agents],
        },
    }
