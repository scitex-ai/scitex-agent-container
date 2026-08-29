"""Artifact gate: assert BY SYMBOL that this SIF is fresh and whole."""

import sys

# noqa placement is deliberate: this import LOOKS unused and is not. The
# probe is an artifact gate that asserts BY SYMBOL that the SIF shipped a
# whole scitex_cards, so the bare import IS the assertion — ruff F401 reads
# it as dead because nothing references the name, and removing it on that
# advice blinded the gate and reddened test_probe_imports_scitex_cards.
import scitex_cards  # noqa: F401  (the import itself is the check)
from scitex_cards._throughput import WIP_STATUSES

# scitex-cards 0.49.1: the comment-preserving mirror write, CORRECTED.
# Through 0.48.0, comment_task / update_task rebuilt a card from the doc the
# caller happened to hold and DROPPED every comment row that doc had not
# seen — a peer's comment written between your read and your write was
# destroyed silently, with a success report. 0.49.0 added this symbol to fix
# that and indexed its rows POSITIONALLY (row[0]), which is KeyError(0) on
# psycopg's dict_row, so on PostgreSQL every card holding comments became
# READ-ONLY: uncommentable, unupdatable, uncompletable, undeletable. 0.49.1
# reads row["author"]. THE FLOOR IS >=0.49.1 AND MUST NEVER BE >=0.49.0.
#
# THIS IMPORT PROVES PRESENCE, NOT BEHAVIOUR, and that distinction is the
# whole lesson of 2026-08-23: it passed on the broken 0.49.0, because the
# function was there and wrong. Measured that day, five independent gates
# went green on that artifact within one hour — this probe, the master-side
# SYMBOL_PROBE, the Spartan bake's content check, upstream's hasattr check,
# and a 7537-test suite that runs on SQLite where the defect cannot exist.
# So the FLOOR is what excludes the broken release; this import only catches
# a version string that lies; and only a post-deploy write to a card that
# ALREADY HAS a comment proves the path actually runs.
from scitex_cards._mirror_rows import _merge_unseen_comment_rows  # noqa: F401

# scitex-dev 0.56.6: the bounded (origin, seq) oplog-allocation retry.
# Through 0.56.5, Store._append read MAX(seq) ONCE and then inserted, so a
# burst of writers on a SINGLE node collided on the oplog (origin, seq)
# primary key with no bounded retry -- 7/8 and 5/8 failures on the two
# concurrency tests, reproduced three times. 0.56.6 adds this constant and
# the retry loop that uses it, plus an advisory lock around table creation.
#
# THIS IMPORT PROVES PRESENCE, NOT BEHAVIOUR -- the same narrow job as the
# scitex-cards import above, and the same 2026-08-23 lesson. The FLOOR is
# what excludes the releases without the retry; this import only catches a
# version string that lies; and only a concurrent multi-writer append after
# deploy proves the loop actually settles under contention.
#
# The name is PRIVATE -- underscore-prefixed and absent from __all__ -- so
# upstream may rename or inline it with no deprecation, and that would land
# here as a dead bake far from scitex-dev's repo. If this line is what broke
# the build, read scitex_dev/store/_store.py before suspecting the image.
from scitex_dev.store._store import _SEQ_ALLOCATION_ATTEMPTS  # noqa: F401

if "in_progress" not in WIP_STATUSES:
    print(f"FATAL: 'in_progress' missing from WIP_STATUSES: {sorted(WIP_STATUSES)}")
    sys.exit(1)

# Newer than any published sac release => proves the %files-staged source
# tree won the install (no transitive PyPI sac wheel overwrote it).
from scitex_agent_container.runtimes._apptainer_overlay import (
    ensure_overlay_dirs,  # noqa: F401,E402
)

print("OK: artifact symbol probe passed")
