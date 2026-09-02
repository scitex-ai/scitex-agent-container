"""Shared helpers for the ``openai-agents`` SDK runtime path.

Sibling of :mod:`runtimes._sdk_common` for the ``openai`` agent SDK
harness (scitex-todo card ``openai-compat-2``; ``spec.harness: openai``
— see :mod:`config._provider_types` for the two-axis naming-collision
note). Mirrors the concern split established there:

1. **Auth provisioning** — :func:`provision_openai_auth` resolves the
   OpenAI API key the SDK will read from ``OPENAI_API_KEY``.
2. **Workspace resolution** — re-exported verbatim from
   :mod:`runtimes._provider_common` (the openai-compat-1 extraction
   whose whole point was letting THIS module reuse it).

Session-state placement USED TO BE the third concern here, and it is gone
rather than moved: the helper picked a filesystem path for a per-agent
database file, and there is no file to place any more. Conversation
state lives in this host's PostgreSQL, resolved by
:mod:`_state.openai_session_store` — a store target, not a path — so the
question this module answered no longer has a filesystem answer. The
cross-reference went with the function on purpose: documentation pointing
at a deleted symbol is how a reader learns to distrust the prose.

The ``openai-agents`` install is OPTIONAL (``pip install
scitex-agent-container[openai]``): nothing here imports ``agents`` (even
lazily) — importing this module must stay side-effect-free for
Claude-only deployments. The SDK-touching code lives in
:mod:`_runners.openai_session`.

Auth contract (asymmetry with the Anthropic path — deliberate)
---------------------------------------------------------------
The Anthropic-side :func:`~runtimes._sdk_common.provision_anthropic_auth`
NEVER honours a pre-set ``ANTHROPIC_API_KEY`` because a stale dotfiles
export silently shadows the Pro/Max flat-rate OAuth credentials file and
flips billing to pay-per-token. OpenAI has no such second auth rail:
there is no OAuth credentials file to shadow and no flat-rate plan to
lose — the API key IS the one and only auth path, so popping a working
``OPENAI_API_KEY`` would strand otherwise-valid setups for zero safety
gain. The contract here is therefore:

* ``SAC_OPENAI_API_KEY`` set → unconditionally OVERWRITE
  ``OPENAI_API_KEY`` with it (sac-tracked source wins, provenance
  preserved — same spirit as ``SAC_ANTHROPIC_API_KEY``).
* ``SAC_OPENAI_API_KEY`` unset → fall back to a pre-existing
  ``OPENAI_API_KEY`` unchanged.
* Neither → :class:`OpenAISDKCommonError` (fail loud, actionable).
"""

from __future__ import annotations

import os

# Workspace + MCP wiring is provider-agnostic — reuse the openai-compat-1
# extraction verbatim (see runtimes/_provider_common.py). Re-exported so
# the OpenAI runner imports ALL its runtime helpers from this module,
# mirroring how the Claude runner imports everything from _sdk_common.
from ._provider_common import project_runtime_root, resolve_agent_workspace

__all__ = [
    "OpenAISDKCommonError",
    "provision_openai_auth",
    "default_openai_model",
    "resolve_agent_workspace",
    "project_runtime_root",
]

_SAC_OPENAI_KEY_ENV = "SAC_OPENAI_API_KEY"
_OPENAI_KEY_ENV = "OPENAI_API_KEY"
_SAC_OPENAI_MODEL_ENV = "SAC_OPENAI_MODEL"


class OpenAISDKCommonError(RuntimeError):
    """Raised when the OpenAI SDK common helpers cannot satisfy a precondition."""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def provision_openai_auth() -> str:
    """Make sure the ``openai-agents`` SDK can authenticate; return the path used.

    The SDK's default client reads ``OPENAI_API_KEY`` from the process
    env at client-construction time, so provisioning means getting the
    right value into that variable (never logging or returning it).

    Precedence (see the module docstring for the asymmetry rationale
    versus the Anthropic contract):

    1. ``SAC_OPENAI_API_KEY`` set → mirrored into ``OPENAI_API_KEY``
       (overwrites any pre-existing value) → returns ``"sac_env"``.
    2. ``OPENAI_API_KEY`` already set → left untouched → returns
       ``"process_env"``.
    3. Neither → raises :class:`OpenAISDKCommonError`.
    """
    sac_value = os.environ.get(_SAC_OPENAI_KEY_ENV)
    if sac_value:
        os.environ[_OPENAI_KEY_ENV] = sac_value
        return "sac_env"

    if os.environ.get(_OPENAI_KEY_ENV):
        return "process_env"

    raise OpenAISDKCommonError(
        f"no OpenAI auth available — export {_SAC_OPENAI_KEY_ENV} "
        f"(preferred; sac-tracked) or {_OPENAI_KEY_ENV}. The "
        "openai-agents SDK cannot open a session without an API key."
    )


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def default_openai_model() -> str | None:
    """Return the operator-configured default OpenAI model, or ``None``.

    ``None`` means "let the ``openai-agents`` SDK pick its own default"
    — we deliberately do NOT hardcode a model id here so sac never
    silently pins the fleet to a stale generation. Operators set
    ``SAC_OPENAI_MODEL`` (env) to steer every OpenAI-backed session on
    a host; per-session ``model=`` arguments win over this.
    """
    value = os.environ.get(_SAC_OPENAI_MODEL_ENV, "").strip()
    return value or None
