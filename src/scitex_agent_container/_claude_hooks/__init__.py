"""Claude Code hooks as a MEASURED, DECLARABLE guarantee.

Named ``_claude_hooks`` and not ``_hooks`` on purpose: ``spec.hooks`` already
exists and means something entirely different (``pre_start`` / ``post_stop``
lifecycle commands, run by ``_lifecycle._hook_runner`` on the host). These are
Claude Code's own hooks — the ``~/.claude/hooks/<event>/<script>`` guards that
fire around the agent's tool calls. Two namespaces, one word; the package name
is where the ambiguity gets resolved rather than in every reader's head.

* :mod:`._inventory` — what is actually armed, measured IN the container.
* :mod:`._floor` — what the spec DECLARED must be armed, and the three-valued
  comparison (satisfied / not satisfied / could-not-tell).
* :mod:`._report` — the cross-package standard health shape for reading it.
* :mod:`._gate` — the refusal, with one named override.
"""

from __future__ import annotations

from ._errors import MissingRequiredHooks
from ._floor import (
    FloorVerdict,
    declared_floor,
    evaluate_floor,
    flatten_floor,
    measurement_site,
    unknown_event_dirs,
)
from ._gate import ALLOW_ENV, ALLOW_FLAG, check_required_hooks, missing_hooks_lines
from ._inventory import HOOK_EVENT_DIRS, HookInventory, hooks_root, inventory_hooks
from ._report import hooks_health, render_hooks_text

__all__ = [
    "ALLOW_ENV",
    "ALLOW_FLAG",
    "HOOK_EVENT_DIRS",
    "FloorVerdict",
    "HookInventory",
    "MissingRequiredHooks",
    "check_required_hooks",
    "declared_floor",
    "evaluate_floor",
    "flatten_floor",
    "hooks_health",
    "hooks_root",
    "inventory_hooks",
    "measurement_site",
    "missing_hooks_lines",
    "render_hooks_text",
    "unknown_event_dirs",
]
