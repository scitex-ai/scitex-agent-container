"""Artifact gate: assert BY SYMBOL that this SIF is fresh and whole."""

import sys

# noqa placement is deliberate: this import LOOKS unused and is not. The
# probe is an artifact gate that asserts BY SYMBOL that the SIF shipped a
# whole scitex_cards, so the bare import IS the assertion — ruff F401 reads
# it as dead because nothing references the name, and removing it on that
# advice blinded the gate and reddened test_probe_imports_scitex_cards.
import scitex_cards  # noqa: F401  (the import itself is the check)
from scitex_cards._throughput import WIP_STATUSES

# scitex-cards 0.49.0: the comment-preserving mirror write. Below it,
# comment_task / update_task rewrite card_json from the copy the caller
# holds and DROP every comment row that copy has not seen — a peer's
# comment written between your read and your write is destroyed silently.
# The bare `import scitex_cards` above cannot catch this: 0.48.0 imports
# perfectly. Measured 2026-08-23 against both artifacts — defined in
# 0.49.0's _mirror_rows (and in its __all__), absent from every file of
# 0.48.0 — so this import discriminates the exact defect.
from scitex_cards._mirror_rows import _merge_unseen_comment_rows  # noqa: F401

if "in_progress" not in WIP_STATUSES:
    print(f"FATAL: 'in_progress' missing from WIP_STATUSES: {sorted(WIP_STATUSES)}")
    sys.exit(1)

# Newer than any published sac release => proves the %files-staged source
# tree won the install (no transitive PyPI sac wheel overwrote it).
from scitex_agent_container.runtimes._apptainer_overlay import (
    ensure_overlay_dirs,  # noqa: F401,E402
)

print("OK: artifact symbol probe passed")
