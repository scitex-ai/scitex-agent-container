"""Option builders for :mod:`.codex_session` — spec/env → SDK arguments.

Split out of ``codex_session`` to keep that module under the 512-line
cap, and because this is a genuinely separate responsibility: turning
sac's configuration surface (constructor args + ``SAC_CODEX_*`` env)
into the exact keyword arguments ``openai-codex`` 0.144.4 accepts.

Both builders take the ``openai_codex`` MODULE as their first argument
rather than importing it. That keeps the optional dependency lazy
exactly where :mod:`.codex_session` already made it lazy (a Claude-only
deployment must be able to import every sac module), and it makes both
functions trivially testable with a stub module object — no SDK, no
subprocess, no network.

SDK signatures these mirror (read off the installed 0.144.4 wheel)::

    CodexConfig(codex_bin=None, launch_args_override=None,
                config_overrides=(), cwd=None, env=None,
                client_name='codex_python_sdk', ...)

    AsyncCodex.thread_start(*, approval_mode=ApprovalMode.auto_review,
        base_instructions=None, config=None, cwd=None,
        developer_instructions=None, ephemeral=None, model=None,
        model_provider=None, personality=None, sandbox=None, ...)

    AsyncCodex.thread_resume(thread_id, *, approval_mode=None,
        base_instructions=None, config=None, cwd=None,
        developer_instructions=None, model=None, model_provider=None,
        personality=None, sandbox=None, service_tier=None)

:func:`resolve_thread_options` deliberately emits only keys BOTH accept,
so the same mapping serves the start and the resume path.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "SAC_CODEX_MODEL_ENV",
    "SAC_CODEX_MODEL_PROVIDER_ENV",
    "SAC_CODEX_SANDBOX_ENV",
    "build_codex_config",
    "resolve_sandbox",
    "resolve_thread_options",
]

#: Env overrides. Named ``SAC_*`` so they cannot collide with the codex
#: binary's own env surface — sac configuration, not codex configuration.
SAC_CODEX_MODEL_ENV = "SAC_CODEX_MODEL"
SAC_CODEX_MODEL_PROVIDER_ENV = "SAC_CODEX_MODEL_PROVIDER"
SAC_CODEX_SANDBOX_ENV = "SAC_CODEX_SANDBOX"

#: Accepted ``sandbox`` spellings → the ``Sandbox`` enum member NAME.
#: Both the wire value ("read-only") and the python spelling
#: ("read_only") are accepted because a spec author will reasonably
#: write either, and silently ignoring an unrecognised one would hand
#: the agent a different sandbox than it asked for.
_SANDBOX_ALIASES = {
    "read-only": "read_only",
    "read_only": "read_only",
    "workspace-write": "workspace_write",
    "workspace_write": "workspace_write",
    "full-access": "full_access",
    "full_access": "full_access",
}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def build_codex_config(codex_mod: Any, **kwargs: Any) -> Any:
    """Build the SDK's ``CodexConfig`` from sac's launch arguments.

    ``codex_bin=None`` leaves the bundled ``openai-codex-cli-bin``
    binary in charge (the SDK resolves it itself), which is what a
    container install wants — the wheel put the executable in
    site-packages and nothing needs to be found on ``PATH``.

    ``config_overrides`` are the codex ``--config key=value`` flags, the
    surface that carries ``[model_providers.*]`` routing (base_url +
    wire_api) for a self-hosted endpoint.
    """
    codex_bin = kwargs.get("codex_bin")
    cwd = kwargs.get("cwd")
    overrides: Sequence[str] = kwargs.get("config_overrides") or ()
    return codex_mod.CodexConfig(
        codex_bin=codex_bin,
        cwd=cwd,
        config_overrides=tuple(overrides),
        client_name="scitex_agent_container",
    )


def resolve_sandbox(codex_mod: Any, sandbox: str | None) -> Any | None:
    """Map a sandbox spelling to the SDK's ``Sandbox`` enum member.

    ``None``/empty (after the ``SAC_CODEX_SANDBOX`` fallback) returns
    ``None``, leaving codex's own default in charge. An UNRECOGNISED
    spelling raises :class:`ValueError` naming the accepted set — a typo
    must not silently downgrade (or upgrade) an agent's sandbox.
    """
    raw = (sandbox or _env(SAC_CODEX_SANDBOX_ENV) or "").strip().lower()
    if not raw:
        return None
    member = _SANDBOX_ALIASES.get(raw)
    if member is None:
        raise ValueError(
            f"unknown codex sandbox {raw!r}: must be one of "
            f"{sorted(set(_SANDBOX_ALIASES))} "
            f"(or unset to keep codex's own default). Refusing to guess — "
            "a mis-spelled sandbox would run the agent at a privilege "
            "level nobody asked for."
        )
    return getattr(codex_mod.Sandbox, member)


def resolve_thread_options(codex_mod: Any, **kwargs: Any) -> dict[str, Any]:
    """Build the kwargs shared by ``thread_start`` and ``thread_resume``.

    Only keys BOTH methods accept are emitted, so one mapping serves the
    fresh-start and the resume path. Every value is omitted when it
    resolves empty, leaving the SDK's own default in charge rather than
    passing an explicit ``None`` that a future SDK release might treat
    as "clear this".

    Precedence for each of model / model_provider / sandbox: the
    explicit argument → the matching ``SAC_CODEX_*`` env → omitted.
    """
    options: dict[str, Any] = {}

    model = (kwargs.get("model") or _env(SAC_CODEX_MODEL_ENV) or "").strip()
    if model:
        options["model"] = model

    provider = (
        kwargs.get("model_provider") or _env(SAC_CODEX_MODEL_PROVIDER_ENV) or ""
    ).strip()
    if provider:
        options["model_provider"] = provider

    cwd = kwargs.get("cwd")
    if cwd:
        options["cwd"] = str(cwd)

    instructions = kwargs.get("instructions")
    if instructions:
        options["developer_instructions"] = str(instructions)

    sandbox = resolve_sandbox(codex_mod, kwargs.get("sandbox"))
    if sandbox is not None:
        options["sandbox"] = sandbox

    return options
