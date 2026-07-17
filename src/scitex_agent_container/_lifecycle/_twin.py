"""Twin-agent spawning — the single import surface (facade).

A TWIN is a NEW agent spawned FROM a PARENT that INHERITS the parent's LIVE
conversation transcript at birth, then diverges. The parent never stops — a
twin is how an agent splits off context-carrying work without pausing its
own loop. Lifetime is independent of the primitive: an ephemeral triage
twin (``restart.policy: never``) and a persistent companion
(``restart.policy: always``) are the SAME mechanism, different lifetime.
Full design: docs/adr/0019 (+ its 2026-07-17 amendment) and the
``33_twin-spawning`` skill.

This module holds NO logic. It re-exports the three cohesive halves so that
``from ._twin import ...`` remains the one import surface for twin logic
(ADR-0019's "twin logic is isolated in ``_lifecycle/_twin.py``"), while each
concern stays independently readable and under the line cap:

* :mod:`._twin_identity` — WHO the twin is: the ``--tag`` naming algebra, the
  deterministic session uuid, the boot identity gate, and the twin error
  hierarchy. The leaf: the other two import it, never the reverse.
* :mod:`._twin_derive` — COMMAND-TIME: pure spec-doc transforms run by the
  ``sac agents twin`` CLI + the ``agent_twin`` MCP tool before the spawn POST.
* :mod:`._twin_seed` — BOOT-TIME: the host-side pre-start step that gates
  identity, forks the twin's first session off the parent's live one, and
  copies the transcript in.

IDENTITY SPLIT — safety-critical (author = twin, owner = parent): see
:mod:`._twin_derive` for the full contract and why owner=parent cannot be
enforced from env.
"""

from __future__ import annotations

from ._twin_derive import (
    build_twin_boot_kick,
    derive_twin_spec,
    prepare_twin_spawn,
)
from ._twin_identity import (
    SELF_NAME_ENV,
    TODO_AGENT_ENV,
    TWIN_NAME_INFIX,
    TWIN_PARENT_ENV,
    TWIN_SESSION_NAMESPACE,
    TwinIdentityError,
    TwinSeedError,
    assert_twin_identity,
    resolve_twin_name,
    twin_name_for_tag,
    twin_session_uuid,
    validate_twin_tag,
)
from ._twin_seed import seed_twin_from_parent

__all__ = [
    "SELF_NAME_ENV",
    "TODO_AGENT_ENV",
    "TWIN_NAME_INFIX",
    "TWIN_PARENT_ENV",
    "TWIN_SESSION_NAMESPACE",
    "TwinIdentityError",
    "TwinSeedError",
    "assert_twin_identity",
    "build_twin_boot_kick",
    "derive_twin_spec",
    "prepare_twin_spawn",
    "resolve_twin_name",
    "seed_twin_from_parent",
    "twin_name_for_tag",
    "twin_session_uuid",
    "validate_twin_tag",
]
