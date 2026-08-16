"""Artifact gate: assert BY SYMBOL that this SIF is fresh and whole."""

import sys

import scitex_cards  # was scitex_todo until 2026-08-16; that shim is now DELETED
from scitex_cards._throughput import WIP_STATUSES

if scitex_todo is not scitex_cards:
    print("FATAL: scitex_todo shim is not scitex_cards — two module trees baked")
    sys.exit(1)
if "in_progress" not in WIP_STATUSES:
    print(f"FATAL: 'in_progress' missing from WIP_STATUSES: {sorted(WIP_STATUSES)}")
    sys.exit(1)

# Newer than any published sac release => proves the %files-staged source
# tree won the install (no transitive PyPI sac wheel overwrote it).
from scitex_agent_container.runtimes._apptainer_overlay import (
    ensure_overlay_dirs,  # noqa: F401,E402
)

print("OK: artifact symbol probe passed")
