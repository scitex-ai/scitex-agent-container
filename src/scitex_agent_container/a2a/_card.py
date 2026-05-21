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
    # v3 rejected `spec.skills` (skills moved to to_home/.claude/skills/).
    # Operator-declared skill IDs now come via metadata.labels.skills as a
    # CSV; we still tolerate the legacy spec.skills.required for any v2
    # YAML that reaches the projector before validation strips it.
    skills_csv = labels.get("skills", "") or ""
    label_skills = [s.strip() for s in skills_csv.split(",") if s.strip()]
    legacy_skills = (spec.get("skills") or {}).get("required") or []
    required_skills = list(label_skills) + list(legacy_skills)
    role = labels.get("role", "agent")
    function = labels.get("function", "")

    agent_base = f"{base}/agents/{name}"

    # ADR-0004 — surface sac MCP push channel on the v1 AgentCard.
    # `spec.claude.channels: [server:sac]` means an in-session MCP push
    # subscriber is attached (the `sac mcp channel` sidecar consuming
    # `/agents/<name>/inbox/stream`). This is orthogonal to A2A's
    # task-level pushNotifications (`tasks/pushNotificationConfig/*`)
    # so we advertise it under `capabilities.extensions[]` per the
    # v1 spec, not by overloading `pushNotifications`.
    claude_block = spec.get("claude") or {}
    declared_channels = list(claude_block.get("channels") or [])
    has_sac_channel = any(
        isinstance(c, str) and c.strip() == "server:sac" for c in declared_channels
    )
    extensions: list[dict[str, Any]] = []
    if has_sac_channel:
        extensions.append(
            {
                "uri": "https://scitex.ai/a2a/extensions/sac-push-channel/v1",
                "description": (
                    "In-session MCP push: `sac mcp channel` subscribes to "
                    "`/agents/<name>/inbox/stream` and delivers events as "
                    "`notifications/claude/channel` to the agent's Claude "
                    "session."
                ),
                "required": False,
                "params": {
                    "sse_path": f"/agents/{name}/inbox/stream",
                    "mcp_tools": [
                        "a2a_send",
                        "a2a_reply",
                        "a2a_ack",
                        "a2a_peers",
                        "a2a_inbox",
                    ],
                },
            }
        )

    return {
        "name": name,
        "description": _read_description(name, v3),
        "version": v3.get("apiVersion", "scitex-agent-container/v3"),
        # ADR-0004 — match A2A v1 AgentCard (lf/a2a/v1 proto):
        # supportedInterfaces[] is REQUIRED; protocolBinding values
        # are "JSONRPC" | "GRPC" | "HTTP+JSON" (proto-canonical).
        "supportedInterfaces": [
            {
                "url": agent_base,
                "protocolBinding": "HTTP+JSON",
                "tenant": name,
                "protocolVersion": "1.0",
            }
        ],
        "provider": {
            "organization": labels.get("team", "scitex-agent-container"),
            "url": "https://scitex.ai",
        },
        "capabilities": {
            "streaming": True,
            # ``pushNotifications`` reflects whether the agent provides a
            # push mechanism AT ALL — true when sac MCP is wired (the
            # SSE + MCP channel surfaced under ``extensions[]``). The
            # specific flavor (sac SSE + MCP vs. A2A task-level webhook)
            # is described by the extension entry; clients that need a
            # particular mechanism should branch on that, not on this
            # boolean alone.
            "pushNotifications": has_sac_channel,
            "extendedAgentCard": False,
            "extensions": extensions,
        },
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
            # D3 — structured isolation block (see
            # docs/adr/0001-isolation-hardening.md). External
            # verifiers (Clew, orochi attestation) read these booleans to
            # attest specific properties; ``level`` is the human shorthand.
            "isolation": _isolation_block(spec),
        },
    }


# ---------------------------------------------------------------------------
# D3 isolation block helpers — pure functions of the YAML dict.
# ---------------------------------------------------------------------------


def _ap(spec: dict[str, Any]) -> dict[str, Any]:
    return spec.get("apptainer") or {}


def _relaxed(spec: dict[str, Any]) -> bool:
    return bool(_ap(spec).get("relaxed", False))


def _raw_args(spec: dict[str, Any]) -> list[str]:
    return list(_ap(spec).get("raw_args") or [])


def _has_flag(spec: dict[str, Any], flag: str) -> bool:
    return any(a == flag for a in _raw_args(spec))


def _has_overlay(spec: dict[str, Any]) -> bool:
    return bool((_ap(spec).get("overlay") or "").strip())


def _hardened(spec: dict[str, Any]) -> bool:
    """A spec is hardened when sac would auto-prepend the flag."""
    return not _relaxed(spec)


def _has_writable_tmpfs(spec: dict[str, Any]) -> bool:
    # sac auto-prepends --writable-tmpfs when (not relaxed) AND (no overlay)
    # AND operator didn't already declare it.
    if _has_flag(spec, "--writable-tmpfs"):
        return True
    if _relaxed(spec):
        return False
    return not _has_overlay(spec)


def _binds(spec: dict[str, Any]) -> list[str]:
    return list(_ap(spec).get("binds") or [])


def _binds_count(spec: dict[str, Any]) -> int:
    return len(_binds(spec))


def _binds_writable_count(spec: dict[str, Any]) -> int:
    """Count binds NOT carrying ``:ro`` (default mode is rw)."""
    n = 0
    for b in _binds(spec):
        # Bind shape: "src:dst[:mode]" — split on ":" from the right.
        parts = str(b).rsplit(":", 1)
        mode = parts[1].strip() if len(parts) == 2 else ""
        if mode != "ro":
            n += 1
    return n


def _preflight_allowed(spec: dict[str, Any]) -> list[str]:
    """``spec.apptainer.preflight_allow`` — empty until the field lands."""
    return list(_ap(spec).get("preflight_allow") or [])


def _isolation_level(spec: dict[str, Any]) -> str:
    """``relaxed`` | ``custom`` | ``hardened``."""
    if _relaxed(spec):
        return "relaxed"
    # custom if any hardened booleans are disabled OR preflight_allowed non-empty.
    if _preflight_allowed(spec):
        return "custom"
    return "hardened"


def _isolation_block(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "level": _isolation_level(spec),
        "containall": _has_flag(spec, "--containall") or _hardened(spec),
        "cleanenv": _has_flag(spec, "--cleanenv") or _hardened(spec),
        "writable_tmpfs": _has_writable_tmpfs(spec),
        "preflight_passed": ([] if _relaxed(spec) else ["uid-nonzero", "no-host-home"]),
        "preflight_allowed": _preflight_allowed(spec),
        "binds_count": _binds_count(spec),
        "binds_writable_count": _binds_writable_count(spec),
    }


def project_card_proto(name: str, v3: dict[str, Any], base_url: str) -> AgentCard:
    """Project a single agent's v3 YAML into the SDK's protobuf ``AgentCard``.

    SDK 1.0.x's :class:`AgentCard` is a protobuf message (not pydantic),
    and only accepts a strict subset of the dict fields we serve at
    ``/.well-known/agent-card.json``. This helper builds the proto card the
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
    """Project a fleet-level v1.0-shaped AgentCard listing ``agents``.

    A2A v1.0 doesn't define a multi-agent directory primitive — the
    well-known is single-agent. We serve a v1-shaped card here anyway
    so spec-strict clients don't reject it; the per-agent listing
    lives under the sac extension namespace
    (``x-scitex-agent-container.agents[]``). Spec-aware sac clients
    walk that array to reach each member's
    ``/agents/<name>/.well-known/agent-card.json``.
    """
    base = base_url.rstrip("/")
    return {
        "name": "scitex-agent-container",
        "description": description
        or "scitex-agent-container fleet — A2A protocol surface.",
        # Match the per-agent `version` convention (the YAML's
        # ``apiVersion: scitex-agent-container/v3``) — both fleet and
        # member cards now use the ``v<N>`` prefix so clients can parse
        # them with the same regex.
        "version": "scitex-agent-container/v1",
        # v1: no top-level url; binding URLs live under supportedInterfaces[].
        "supportedInterfaces": [
            {
                "url": base,
                "protocolBinding": "HTTP+JSON",
                "tenant": "scitex-agent-container",
                "protocolVersion": "1.0",
            }
        ],
        "provider": {
            "organization": "scitex-agent-container",
            "url": "https://scitex.ai",
        },
        # v1 capabilities — no stateTransitionHistory (v0.x removed).
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
            "extensions": [
                {
                    "uri": "https://scitex.ai/a2a/extensions/sac-fleet/v1",
                    "description": (
                        "sac multi-agent fleet listing. Members are "
                        "advertised under `x-scitex-agent-container.agents[]`; "
                        "each member has its own v1 AgentCard at "
                        "/agents/<name>/.well-known/agent-card.json."
                    ),
                    "required": False,
                    "params": {
                        "members_path": "/agents/",
                        "member_card_path": (
                            "/agents/<name>/.well-known/agent-card.json"
                        ),
                    },
                }
            ],
        },
        # v1 drops top-level `authentication`. We advertise no auth by
        # simply omitting both `securitySchemes` and `securityRequirements`.
        "defaultInputModes": list(DEFAULT_INPUT_MODES),
        "defaultOutputModes": list(DEFAULT_OUTPUT_MODES),
        "skills": [
            {
                "id": "sac.fleet",
                "name": "fleet",
                "description": ("sac-served fleet — see /agents/ for members."),
                "tags": ["multi-agent", "scitex-agent-container"],
            }
        ],
        "x-scitex-agent-container": {
            # Each member entry mirrors the v1 AgentCard shape: binding
            # URLs live under ``supportedInterfaces[]`` (ADR-0004 D11),
            # not on a top-level ``url`` (which v1 dropped).
            "agents": [
                {
                    "name": n,
                    "supportedInterfaces": [
                        {
                            "url": f"{base}/agents/{n}",
                            "protocolBinding": "HTTP+JSON",
                            "tenant": n,
                            "protocolVersion": "1.0",
                        }
                    ],
                }
                for n in agents
            ],
        },
    }


# ---------------------------------------------------------------------------
# v1 schema validator — every served card MUST pass.
# ---------------------------------------------------------------------------


class CardSchemaError(ValueError):
    """A served AgentCard dict didn't match the A2A v1 proto schema.

    Raised by :func:`validate_card_v1` and propagated by the route
    handlers as a 500 with a clear server-log entry — we never serve a
    silently-broken card.
    """


def validate_card_v1(card: dict[str, Any]) -> None:
    """Validate ``card`` against the A2A v1 ``AgentCard`` proto.

    sac extension keys (``x-*`` and ``x-scitex-agent-container``) are
    stripped before the proto check — the proto rejects unknown fields
    and our extension namespace is intentionally outside the proto. The
    stripped subset must still parse cleanly; if not, that's a real
    schema bug in our projection code.
    """
    sanitized = {
        k: v
        for k, v in card.items()
        if not (k == "x-scitex-agent-container" or k.startswith("x-"))
    }
    try:
        ParseDict(sanitized, AgentCard())
    except Exception as exc:  # stx-allow: fallback (reason: surface every schema mismatch as a clear error rather than a silent malformed card)
        raise CardSchemaError(
            f"served AgentCard failed A2A v1 schema validation: {exc}"
        ) from exc
