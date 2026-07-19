"""Artifact gate: assert BY SYMBOL that this SIF is fresh and whole."""

import sys

import scitex_cards  # noqa: F401
from scitex_cards._throughput import WIP_STATUSES

# This gate used to also import the wheel's pre-rename module alias and assert
# it resolved to scitex_cards. sac no longer imports that alias anywhere, so
# asserting it here would gate the bake on something we do not use — and would
# fail the bake the day scitex-cards drops it. The load-bearing assertions
# stay: the import above proves scitex_cards is installed, and WIP_STATUSES
# below proves it is a version carrying the WIP gate rather than a fossil.
if "in_progress" not in WIP_STATUSES:
    print(f"FATAL: 'in_progress' missing from WIP_STATUSES: {sorted(WIP_STATUSES)}")
    sys.exit(1)

# Newer than any published sac release => proves the %files-staged source
# tree won the install (no transitive PyPI sac wheel overwrote it).
from scitex_agent_container.runtimes._apptainer_overlay import (
    ensure_overlay_dirs,  # noqa: F401,E402
)

print("OK: artifact symbol probe passed")
