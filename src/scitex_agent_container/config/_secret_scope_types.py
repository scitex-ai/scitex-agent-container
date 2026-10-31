"""The SECRET-SCOPE axis — ``spec.secret_scope``: how much of the operator
secret pool reaches the agent's runtime environment.

Two tiers, declared per spec (operator directive 2026-10-22):

  * ``full``    (default) — every pool-file-defined secret lands in the
    agent's profile env. Development agents: they need *all*
    authentications and permissions to do daily work.
  * ``capsule`` — declared inference keys + delivery tokens (GH_TOKEN,
    CCT bot token) only. Insulated environments (e.g. paper-scitex-clew
    capsule solvers): the capsule sees exactly what its spec declares.

LAYER CLARIFICATION — this axis is NOT the apptainer container posture:
container defaults (binds, caps, isolation) stay limited for every
agent. This axis governs only which pool secrets are MATERIALISED into
the agent's env files at launch. Values still never appear in specs
(specs reach GitHub); only names are declared here, values resolve from
the trusted pool at launch.

POSTURE — defaulted, not required-explicit, deliberately (the same
rationale as ``_residency_types``: a NEW axis with no legacy spelling;
requiring it would red-start the fleet for declaring nothing new). An
ILLEGAL value still fails loud at validation time, naming the closed
set.
"""

from __future__ import annotations

from typing import Literal, Mapping

__all__ = [
    "AGENT_SECRET_SCOPES",
    "AgentSecretScope",
    "CAPSULE",
    "DEFAULT_AGENT_SECRET_SCOPE",
    "FULL",
    "SECRET_SCOPE_KEY",
    "declared_secret_scope",
    "is_known_secret_scope",
    "list_secret_scopes",
    "resolve_spec_secret_scope",
    "secret_scope_value_error",
]

#: The top-level spec key for this axis. No legacy alias — the axis is
#: new; nothing in the corpus ever spelled it differently.
SECRET_SCOPE_KEY = "secret_scope"

AgentSecretScope = Literal["full", "capsule"]

#: Development agents get the whole trusted pool in their profile env.
FULL: AgentSecretScope = "full"

#: Insulated environments get declared keys + delivery tokens only.
CAPSULE: AgentSecretScope = "capsule"

#: Matches every live development agent today; capsule is the opt-in.
DEFAULT_AGENT_SECRET_SCOPE: AgentSecretScope = FULL

#: The closed set. A typo is a validation error naming this set.
AGENT_SECRET_SCOPES: tuple[str, ...] = (FULL, CAPSULE)


def is_known_secret_scope(name: str) -> bool:
    """True when ``name`` is a recognized secret scope."""
    return name in AGENT_SECRET_SCOPES


def list_secret_scopes() -> list[str]:
    """The recognized secret scopes, sorted — for fail-loud error messages."""
    return sorted(AGENT_SECRET_SCOPES)


def _stated(spec: Mapping, key: str) -> str | None:
    """The STATED value of ``spec[key]``, or ``None`` for "no opinion".

    Same identity rules as the harness axis (``_harness_types._stated``):
    absent, present-with-null, and present-but-empty/whitespace all mean
    "declares nothing". Normalised (stripped + lowercased) — the
    VALUE-legality check owns the casing diagnostic, this function only
    owns identity.
    """
    if not isinstance(spec, Mapping) or key not in spec:
        return None
    raw = spec[key]
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return text or None


def declared_secret_scope(spec: Mapping) -> str | None:
    """The secret scope this spec states, or ``None`` when it states none."""
    return _stated(spec, SECRET_SCOPE_KEY)


def resolve_spec_secret_scope(spec: Mapping) -> str:
    """The secret scope for this spec, defaulting to ``full``.

    The default is the documented fleet posture (every live development
    agent today runs full), not a silent guess — see the module docstring
    for why absence is legal while the required-explicit map exists.
    """
    return declared_secret_scope(spec) or DEFAULT_AGENT_SECRET_SCOPE


def secret_scope_value_error(spec: Mapping) -> list[str]:
    """List-form VALUE check for ``validate_raw``: 0 or 1 message.

    Runs on the WRITTEN value (not the normalised resolution) so the
    error names the exact line the operator has to edit — the same
    posture as the harness value check.
    """
    value = spec.get(SECRET_SCOPE_KEY) if isinstance(spec, Mapping) else None
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    if isinstance(value, str) and is_known_secret_scope(value.strip().lower()):
        return []
    return [
        f"spec.{SECRET_SCOPE_KEY} must be one of {list_secret_scopes()} (got {value!r})"
    ]
