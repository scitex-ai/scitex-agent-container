"""Persistent Codex app-server runtime dispatched through Apptainer."""

from __future__ import annotations

from ..config import AgentConfig
from .openai_session import OpenAISessionRuntime

__all__ = ["CodexSessionRuntime"]


def _container_runtime_for(config: AgentConfig):
    """Resolve only the runtime modes owned by the Codex SDK descriptor."""
    runtime = getattr(config, "runtime", "") or ""
    from ..config._harness_registry import CODEX_SDK, runtime_spellings_for

    if runtime in runtime_spellings_for(CODEX_SDK):
        from ._apptainer_runtime import ApptainerContainerRuntime

        return ApptainerContainerRuntime()
    return None


class CodexSessionRuntime(OpenAISessionRuntime):
    """Run SAC's persistent Codex app-server session daemon in Apptainer.

    The inherited adapter owns the generic runner lifecycle and ``to_home``
    deployment. The registry-selected inner argv starts
    ``_runners.codex_session``; that daemon routes idle input to ``turn/start``
    and active input to native ``turn/steer``.
    """

    def __init__(self, container_runtime_for=None):
        super().__init__(container_runtime_for or _container_runtime_for)
