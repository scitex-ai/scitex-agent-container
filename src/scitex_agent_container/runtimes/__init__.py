"""Runtime adapters for agent execution.

``ClaudeSessionRuntime`` is loaded lazily via module ``__getattr__``
(PEP 562). Eager-importing it would also pull in the daemon runner
module (``_runners/claude_session.py``); when the runner is then
re-executed via ``python -m scitex_agent_container._runners.claude_session``,
runpy issues a ``RuntimeWarning`` because the module is already in
``sys.modules``. Deferring the adapter import breaks that cycle —
the runner subprocess never imports the adapter, so the warning
never fires.
"""

from .base import RuntimeBase
from .docker import DockerRuntime
from .podman import PodmanRuntime

__all__ = [
    "ClaudeSessionRuntime",
    "DockerRuntime",
    "PodmanRuntime",
    "RuntimeBase",
]


def __getattr__(name: str):
    if name == "ClaudeSessionRuntime":
        from .claude_session import ClaudeSessionRuntime

        return ClaudeSessionRuntime
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
