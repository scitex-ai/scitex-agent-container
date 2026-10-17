"""Packaged worktree-policy checker shared by lifecycle and CLI adapters."""

from ._cli import main
from ._policy import PACKAGED_POLICY, default_policy_path, host_policy_path

__all__ = ["PACKAGED_POLICY", "default_policy_path", "host_policy_path", "main"]
