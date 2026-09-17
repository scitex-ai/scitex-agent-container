"""Compile-time registry for secrets consumed from the host pool.

Every name here is tied to a concrete provider consumer in this branch.  The
registry is deliberately code-owned: environment/config values may select one
of these names, but may never add a name.  Adding a provider credential requires
adding its consumer and tests in the same change.
"""

from __future__ import annotations

# Positive authorization only.  Do not replace this with a denylist: process
# runtimes expose an unbounded set of variables that can execute code before a
# child's argv runs.  Do not add speculative credentials without their consumer.
REGISTERED_PROVIDER_SECRET_NAMES = frozenset(
    {
        "COMMAND_CODE_API_KEY",  # Command Code provider API consumer
        "OPENCODE_GO_API_KEY",  # listener OpenCode provider tuple
        "SAC_LOCAL_GPTOSS_KEY",  # deployed Qwen host-name override
        "SCITEX_GENAI_GATEWAY_API_KEY",  # default Qwen/gateway credential
    }
)

__all__ = ["REGISTERED_PROVIDER_SECRET_NAMES"]
