"""Sealed runs (``spec.sandbox``): egress allowlist, env seal, per-run reset, outcome.

See :mod:`._spec` for the contract, :mod:`._layout` for the on-disk layout,
:mod:`.netgw` + :mod:`._proxy` for the egress mechanism (container
``--network none`` + an in-container user/net namespace whose only way out is
a CONNECT relay to a host-side allowlisting proxy — no iptables, netns or
sudo on the host), :mod:`._run` for the per-run lifecycle and
:mod:`._supervisor` for the host-side process that owns one run and
:mod:`._exec` for the plain-command run (``sac sandbox exec``).
"""

from __future__ import annotations

from ._argv import (
    drop_writable_tmpfs,
    is_sandboxed,
    netgw_wrap,
    run_flags,
    sandbox_flags,
    supervised_argv,
)
from ._env_policy import (
    allowed_env_names,
    filter_env,
    seal_profile_env,
    validate_sandbox_config,
)
from ._run import (
    SandboxRunError,
    begin_run,
    current_run,
    end_run,
    finalize_run,
    prepare_run_dir,
)
from ._spec import SandboxSpec, parse_sandbox

__all__ = [
    "SandboxRunError",
    "SandboxSpec",
    "allowed_env_names",
    "begin_run",
    "current_run",
    "drop_writable_tmpfs",
    "end_run",
    "filter_env",
    "finalize_run",
    "is_sandboxed",
    "netgw_wrap",
    "parse_sandbox",
    "prepare_run_dir",
    "run_flags",
    "sandbox_flags",
    "seal_profile_env",
    "supervised_argv",
    "validate_sandbox_config",
]
