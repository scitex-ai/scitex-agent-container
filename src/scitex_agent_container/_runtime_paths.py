"""Single source of truth for the sac runtime base directory.

The runtime base holds every per-run, inode-heavy artefact: the
``state.db``, the file registry, and each agent's ``runtime/<name>/``
state dir (boot logs, heartbeat.json, session.jsonl, home/, tmp
scratch). Historically the base was hard-coded to
``~/.scitex/agent-container/runtime`` via ``os.path.expanduser`` copied
into several modules.

On a GPFS-home HPC node that forces every capsule's per-run runtime onto
the shared fileset, which exhausts its inode budget under a large launch
wave (the sac-runtime-state-hygiene incident: 1.25M+ inodes). This module
introduces ONE resolver so setting a single env var relocates the whole
runtime tree node-local::

    SCITEX_AGENT_CONTAINER_RUNTIME_DIR=$TMPDIR/sac-rt

When the env var is UNSET the default is byte-identical to the historical
path, so back-compat is preserved.
"""

from __future__ import annotations

import os
from pathlib import Path

RUNTIME_DIR_ENV = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"

# Historical default — kept identical so an unset env is a no-op change.
_DEFAULT_RUNTIME_SUBPATH = "~/.scitex/agent-container/runtime"


def runtime_base_dir() -> Path:
    """Return the runtime base dir, honouring the relocation env var.

    Resolution:
      1. ``$SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` if set and non-empty
         (``~`` expanded, made absolute), else
      2. the historical ``~/.scitex/agent-container/runtime`` default.

    Read at call time so a test (or launcher) that sets the env var sees
    it immediately. Callers that need a module-level constant snapshot it
    at import — that mirrors the pre-existing behaviour and stays
    identical when the env is unset.
    """
    env = os.environ.get(RUNTIME_DIR_ENV)
    if env:
        return Path(os.path.abspath(os.path.expanduser(env)))
    return Path(os.path.expanduser(_DEFAULT_RUNTIME_SUBPATH))
