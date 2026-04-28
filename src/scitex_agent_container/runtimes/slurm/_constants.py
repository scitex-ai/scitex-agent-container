"""Hardener constants for the SLURM runtime sbatch wrapper.

These strings are the regression surface asserted by
``tests/test_slurm_runtime.py``. Changing them without updating the test
is a deliberate decision.

Promoted from the retired sbatch_spartan.py (todo#425) and llama-on-slurm's
walltime-auto-resubmit pattern.
"""

from __future__ import annotations

REQUIRED_SHEBANG = "#!/bin/bash"
REQUIRED_STRICT_MODE = "set -euo pipefail"
REQUIRED_XTRACE = "set -x"
REQUIRED_HOLD_DEFAULT = "tail -f /dev/null"
REQUIRED_EXIT_TRAP_MARKER = "[sac/slurm] wrapper exiting"
REQUIRED_USR1_TRAP_MARKER = "_sac_slurm_walltime_handler"

# Heartbeat loop markers — asserted by tests and by operators grepping
# job logs ("is my compute-node heartbeat even running?"). Present only
# when ``spec.slurm.heartbeat.command`` is non-empty.
HEARTBEAT_LOOP_MARKER = "_sac_slurm_heartbeat_loop"
HEARTBEAT_START_MARKER = "[sac/slurm] heartbeat daemon started"


__all__ = [
    "HEARTBEAT_LOOP_MARKER",
    "HEARTBEAT_START_MARKER",
    "REQUIRED_EXIT_TRAP_MARKER",
    "REQUIRED_HOLD_DEFAULT",
    "REQUIRED_SHEBANG",
    "REQUIRED_STRICT_MODE",
    "REQUIRED_USR1_TRAP_MARKER",
    "REQUIRED_XTRACE",
]
