"""Public surface for the SLURM runtime package.

Re-exports ``SlurmRuntime``, the rendering helpers, and all hardener
constants asserted by ``tests/test_slurm_runtime.py`` so existing imports
keep working:

    from scitex_agent_container.runtimes.slurm import (
        SlurmRuntime, render_sbatch_script, render_attach_command,
        REQUIRED_SHEBANG, REQUIRED_USR1_TRAP_MARKER, ...
    )

Submodules:
    _constants — hardener strings (regression surface)
    _state     — per-agent JSON state file
    _heartbeat — compute-node heartbeat shell-fragment generator
    _render    — sbatch wrapper rendering
    _runtime   — SlurmRuntime class + scitex-hpc dual-write helpers
"""

from __future__ import annotations

import subprocess  # noqa: F401 — re-exported for test monkeypatching

from ._constants import (
    HEARTBEAT_LOOP_MARKER,
    HEARTBEAT_START_MARKER,
    REQUIRED_EXIT_TRAP_MARKER,
    REQUIRED_HOLD_DEFAULT,
    REQUIRED_SHEBANG,
    REQUIRED_STRICT_MODE,
    REQUIRED_USR1_TRAP_MARKER,
    REQUIRED_XTRACE,
)
from ._render import render_attach_command, render_sbatch_script
from ._runtime import (
    SlurmRuntime,
    _maybe_clear_hpc_reservation,
    _maybe_register_hpc_reservation,
    _parse_sbatch_jobid,
)
from ._state import _clear_state, _read_state, _state_dir, _state_path, _write_state


def _pkg_lookup():
    """Return this package module, used by ``_runtime.SlurmRuntime`` for
    late-binding attribute lookups so test ``monkeypatch.setattr`` against
    the package namespace (``slurm_mod.subprocess``,
    ``slurm_mod._maybe_register_hpc_reservation``) takes effect.
    """
    import sys

    return sys.modules[__name__]


__all__ = [
    "HEARTBEAT_LOOP_MARKER",
    "HEARTBEAT_START_MARKER",
    "REQUIRED_EXIT_TRAP_MARKER",
    "REQUIRED_HOLD_DEFAULT",
    "REQUIRED_SHEBANG",
    "REQUIRED_STRICT_MODE",
    "REQUIRED_USR1_TRAP_MARKER",
    "REQUIRED_XTRACE",
    "SlurmRuntime",
    "render_attach_command",
    "render_sbatch_script",
]
