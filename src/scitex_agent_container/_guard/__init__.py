#!/usr/bin/env python3
# File: src/scitex_agent_container/_guard/__init__.py

"""Standing guards over delegated code changes.

The first inhabitant is the UNREQUESTED-DELETION guard. It answers one
question mechanically: *did this change remove anything nobody asked for?*

Why it exists
=============
A local model, asked to ADD a function, once deleted two classes that a
sibling module imported — and its own summary never mentioned the
deletion. That is the shape being guarded, and it is not hypothetical: the
detector below was built to reproduce it, then run over 36 measured
local-model trials (2 models x 6 difficulty rungs x 3 repetitions). Zero
unrequested deletions occurred across all 36 diffs **with the guard in
place**, which is a statement about the guard, not about the models.

The rule that follows from those trials is the reason this module is not
inside the trials harness any more: a delegated result may be trusted only
after it passes a MECHANICAL gate, never on the strength of the worker's
self-report. A gate reachable by exactly one script is not a gate.

Surface
-------
* :func:`check_deletions` — the whole answer, as one validated
  :class:`DeletionReport`. Never a bool: ``clean``, ``violations`` and
  ``could-not-determine`` are three distinct verdicts, and the third one
  can never be constructed to look like the first.
* :func:`render` — the human table.
* ``sac guard deletions`` — the same thing from any hook, agent or shell,
  with declared exit codes (0 / 3 / 4).

The symbol primitives (:func:`detect_deletions`, :func:`symbol_set`,
:func:`diff_trees`, :func:`added_symbols`) keep the semantics they had in
``scripts/local_model_trials/detectors.py``, which re-exports them from
here so the trials harness keeps working unchanged.
"""

from __future__ import annotations

from ._check import check_deletions
from ._render import render
from ._report import (
    CLEAN,
    EXIT_CLEAN,
    EXIT_UNDETERMINED,
    EXIT_VIOLATIONS,
    UNDETERMINED,
    VERDICTS,
    VIOLATIONS,
    Deletion,
    DeletionReport,
)
from ._symbols import (
    added_symbols,
    detect_deletions,
    diff_trees,
    symbol_locations,
    symbol_set,
)
from ._trees import (
    BaselineUnavailable,
    tree_from_dir,
    tree_from_ref,
    tree_from_worktree,
)

__all__ = [
    "CLEAN",
    "EXIT_CLEAN",
    "EXIT_UNDETERMINED",
    "EXIT_VIOLATIONS",
    "UNDETERMINED",
    "VERDICTS",
    "VIOLATIONS",
    "BaselineUnavailable",
    "Deletion",
    "DeletionReport",
    "added_symbols",
    "check_deletions",
    "detect_deletions",
    "diff_trees",
    "render",
    "symbol_locations",
    "symbol_set",
    "tree_from_dir",
    "tree_from_ref",
    "tree_from_worktree",
]

# EOF
