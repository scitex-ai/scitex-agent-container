"""Required-field map for the explicit-spec ruling (2026-07-21).

WHY THIS EXISTS — operator ruling 2026-07-21, verbatim intent:

  * EVERY field in an agent ``spec.yaml`` must be written explicitly.
    An omitted field is an ERROR at config-load time, with a hint.
  * NO migration machinery, NO warn phase, NO escape-hatch env flag.
    Existing under-specified specs going boot-red at once is ACCEPTED
    and desired ("red start, hard").
  * The structure must leave no option but compliance.

This module is the DATA side: it derives the required YAML-key map from
``dataclasses.fields()`` of the section dataclasses wherever the YAML
key ↔ dataclass field mapping is 1:1, and from explicit alias tables
where it is not (``watchdog.responses.*``, ``restart.backoff.*``,
``python-venv``). The parsers in ``config/_parsers`` remain the SSOT of
which keys exist; the dataclasses remain the SSOT of the value tree.
The raising walker lives in the sibling ``_explicit_validation``.

Each required entry carries the CURRENT default value so the error hint
can emit a paste-ready YAML block that reproduces, field for field, the
exact behaviour the spec had while the field was omitted. Fields whose
absence triggers a LOADER derivation (workdir, claude.session) paste
``null`` — present-but-null keeps the derivation, which is the
behaviour-preserving explicit spelling.

EXCLUDED from the required map, each with its reason:

  * ``spec.host`` / ``spec.hosts`` — mutually exclusive pair; exactly-one
    presence is already enforced (loudly) by ``_placement_validation``.
    A require-ALL map cannot demand both.
  * ``spec.session`` — top-level ergonomic alias of the canonical
    ``spec.claude.session``; requiring both would force a double
    declaration. The canonical nested key is required instead.
  * ``spec.claude.*`` — required only for ``kind: Agent``;
    ``validate_proxy_coupling`` FORBIDS the block on ``kind: AgentProxy``.
  * ``spec.proxy.*`` — required only for ``kind: AgentProxy``; the same
    coupling validator forbids the block on ``kind: Agent``.
  * ``spec.screen`` — legacy inert metadata (only ``screen.name`` is
    read, and it no longer drives a multiplexer); requiring it would
    enshrine a dead field.
  * ``spec.startup`` — listed in ``_KNOWN_SPEC_KEYS`` but materialised
    by NO parser (``getattr(config, "startup", None)`` is always None);
    a dead key cannot be meaningfully required.
  * ``spec.multiplexer`` / ``spec.env-file`` / ``spec.exclude_hooks`` /
    ``spec.exclude_skills`` — read by ``load_v3`` but ABSENT from
    ``_KNOWN_SPEC_KEYS``, so ``validate_raw`` rejects them as unknown;
    requiring them would be a contradiction (red both ways). Flagged
    for operator review.
  * banned/relocated keys stay banned, never required: ``spec.access``,
    ``spec.scheduling``, ``spec.dockerfile``, top-level ``image`` /
    ``env`` / ``model`` / ``mounts``, ``spec.skills``, ``spec.dot_claude``,
    ``spec.remote``, ``metadata.name``, ``apptainer.container_workdir``.
  * list-ITEM internals (``listen[].port``, ``startup_commands[].command``,
    ``apptainer.binds`` entries, ``mcp_servers.*`` bodies,
    ``claude.provider`` dict internals) — the LIST/MAPPING itself is the
    explicit declaration; item shapes are validated by their parsers.
  * ``metadata`` / ``metadata.labels`` — outside ``spec``; free-form
    advisory labels.

PASTE-VALUE OVERRIDES (default shown ≠ value pasted):

  * ``health.method`` — the dataclass default ``multiplexer-alive`` is a
    fossil ``validate_raw`` REJECTS when written; the only writable
    value is ``sdk-alive``, so that is pasted.
  * ``workdir`` / ``claude.session`` — ``null`` pastes the loader
    derivation (per-agent runtime dir / role-derived session), which is
    exactly what omission used to mean.
  * ``proxy.upstream`` — no default exists (the parser hard-requires
    it); the pasted placeholder matches the existing validator hint.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Mapping

from ._acl_types import (
    A2ACommsToggle,
    InboundCommsSpec,
    LineageSpec,
    OutboundCommsSpec,
)
from ._apptainer_spec import ApptainerSpec
from ._harness_types import HARNESS_KEY, LEGACY_HARNESS_KEY
from ._proxy_types import ProxySpec
from ._types import (
    A2ASpec,
    AutonomousSpec,
    ClaudeSpec,
    ContainerSpec,
    ContextManagementConfig,
    HealthSpec,
    HookSpec,
    RestartSpec,
    WatchdogSpec,
)

__all__ = ["RequiredField", "required_fields_for_kind"]

# Sentinel: "no paste override — paste the dataclass default".
_NO_OVERRIDE = object()


@dataclass(frozen=True)
class RequiredField:
    """One required YAML key under ``spec.`` with its hint payload."""

    path: str  # dotted YAML path under spec., e.g. "claude.session"
    type_str: str  # human-readable expected type
    default_repr: str  # the CURRENT default, as shown in the hint
    paste_value: Any  # value emitted in the paste-ready YAML block
    # A DEPRECATED spelling that also satisfies this requirement. The
    # red-start ruling bans a migration PHASE, not a renamed key: a spec
    # that already declares the axis under its old name has written the
    # field, so demanding the new spelling too would be a second
    # declaration of one thing. ``""`` = no alias (the normal case).
    legacy_path: str = ""


def _default_of(field: dataclasses.Field) -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    return field.default_factory()  # type: ignore[misc]


def _section(
    prefix: str,
    dc: type,
    *,
    alias: Mapping[str, str] | None = None,
    exclude: tuple[str, ...] = (),
    paste_overrides: Mapping[str, Any] | None = None,
    default_notes: Mapping[str, str] | None = None,
) -> list[RequiredField]:
    """Derive one section's required keys from ``dataclasses.fields(dc)``.

    ``alias`` maps dataclass field name → YAML key path (relative to
    ``prefix``) where the two differ. ``paste_overrides`` maps YAML key
    path → the value pasted instead of the dataclass default.
    ``default_notes`` appends a clarifying note to the shown default.
    """
    alias = alias or {}
    paste_overrides = paste_overrides or {}
    default_notes = default_notes or {}
    out: list[RequiredField] = []
    for f in dataclasses.fields(dc):
        if f.name in exclude:
            continue
        key = alias.get(f.name, f.name)
        path = f"{prefix}.{key}" if prefix else key
        default = _default_of(f)
        shown = repr(default)
        if key in default_notes:
            shown = f"{shown} ({default_notes[key]})"
        paste = paste_overrides.get(key, _NO_OVERRIDE)
        if paste is _NO_OVERRIDE:
            paste = default
        out.append(
            RequiredField(
                path=path,
                type_str=str(f.type),
                default_repr=shown,
                paste_value=paste,
            )
        )
    return out


def _top_level_fields() -> list[RequiredField]:
    """Hand-authored top-level ``spec.*`` scalars (no 1:1 dataclass)."""
    return [
        RequiredField("runtime", "str", "'tui'", "tui"),
        # The harness axis. ``spec.provider`` is its deprecated alias and
        # satisfies the requirement, so the ~100 specs written before the
        # rename keep loading; the paste-ready hint emits the canonical
        # spelling so anything scaffolded from here is already migrated.
        RequiredField(
            HARNESS_KEY,
            "str",
            "'anthropic'",
            "anthropic",
            legacy_path=LEGACY_HARNESS_KEY,
        ),
        RequiredField(
            "workdir",
            "str | None",
            "None (derived: ~/.scitex/agent-container/runtime/agents/<name>)",
            None,
        ),
        RequiredField("python-venv", "str | list[str]", "''", ""),
        RequiredField("startup_commands", "list", "[]", []),
        RequiredField(
            "startup_prompts",
            "list[str]",
            "[] (empty inherits the generic boot kick)",
            [],
        ),
        RequiredField("listen", "list", "[]", []),
        RequiredField("extensions", "dict", "{}", {}),
        RequiredField("mcp_servers", "dict", "{}", {}),
        RequiredField("user", "str", "''", ""),
        RequiredField("to_home", "str", "'./to_home'", "./to_home"),
    ]


def _both_kinds_fields() -> list[RequiredField]:
    fields = _top_level_fields()
    fields += _section("container", ContainerSpec)
    fields += _section(
        "health",
        HealthSpec,
        paste_overrides={
            # Dataclass default 'multiplexer-alive' is a fossil the
            # validator REJECTS when written; 'sdk-alive' is the only
            # writable value (see module docstring).
            "method": "sdk-alive",
        },
        default_notes={"method": "fossil; write 'sdk-alive'"},
    )
    fields += _section(
        "watchdog",
        WatchdogSpec,
        alias={
            "resp_y_n": "responses.y_n",
            "resp_y_y_n": "responses.y_y_n",
            "resp_waiting": "responses.waiting",
        },
    )
    fields += _section(
        "restart",
        RestartSpec,
        alias={
            "backoff_initial": "backoff.initial",
            "backoff_max": "backoff.max",
            "backoff_multiplier": "backoff.multiplier",
        },
    )
    fields += _section("autonomous", AutonomousSpec)
    fields += _section(
        "apptainer",
        ApptainerSpec,
        # container_workdir is BANNED (removed with spec.access); banned
        # stays banned, never required.
        exclude=("container_workdir",),
    )
    fields += _section("hooks", HookSpec)
    fields += _section("context_management", ContextManagementConfig)
    fields += _section("a2a", A2ASpec)
    fields += _section("comms.outbound", OutboundCommsSpec)
    fields += _section("comms.inbound", InboundCommsSpec)
    fields += _section("comms.a2a", A2ACommsToggle)
    fields += _section("lineage", LineageSpec)
    return fields


def _claude_fields() -> list[RequiredField]:
    return _section(
        "claude",
        ClaudeSpec,
        paste_overrides={
            # null keeps the role-derived session default (continue for
            # coordinator roles, fresh otherwise) — exactly what omission
            # used to mean. Writing 'fresh' here would silently flip
            # coordinators to fresh and lose their working memory.
            "session": None,
        },
        default_notes={
            "session": "null keeps the role-derived default",
            "model": "empty uses the runtime default",
        },
    )


def _proxy_fields() -> list[RequiredField]:
    return _section(
        "proxy",
        ProxySpec,
        paste_overrides={
            # No default exists (the parser hard-requires upstream); the
            # placeholder matches the existing validate_proxy_coupling hint
            # and MUST be edited to the real forwarding target.
            "upstream": "http://127.0.0.1:9000",
        },
        default_notes={"upstream": "no default — set your real upstream"},
    )


def required_fields_for_kind(kind: object) -> tuple[RequiredField, ...]:
    """The full required-key map for a ``kind: Agent|AgentProxy`` spec.

    Unknown kinds get the both-kinds set only — ``validate_raw`` already
    rejects the kind itself with its own error.
    """
    fields = _both_kinds_fields()
    if kind == "Agent":
        fields += _claude_fields()
    elif kind == "AgentProxy":
        fields += _proxy_fields()
    return tuple(fields)
