"""Programmatic surface for the fleet-wide responsiveness doctrine.

The responsiveness doctrine is shipped as a skill markdown file
(``30_responsiveness-background-work.md``) and a CLAUDE.md template
section in the example agent (``examples/agents/full-agent/to_home/
CLAUDE.md``). Both surfaces are static text — they ship to every
agent on next restart via the package's ``_skills`` rollout and the
``to_home`` materialisation.

This module gives test code (and any tooling that wants to verify
the doctrine is materially present in a deployed agent) a single
import-able handle: the canonical title string, the four relaunch
routes, and the operator-wording marker.

The doctrine itself reads:

  Never block the main turn on long-running work. If a Bash command
  would run for more than ~7 seconds, launch it in the background
  and end your turn promptly; the runtime delivers the completion as
  a ``<task-notification>`` on a later turn. The operator's Telegram
  message must NOT wait until your heavy turn finishes — the runner
  reads ONE inbox sequentially. Operator wording (8843 / 8845):
  ``作業中断はしてほしくない``.

Reference: ``30_responsiveness-background-work.md`` (canonical body).
"""

from __future__ import annotations

DOCTRINE_TITLE: str = "Responsiveness — keep the main turn free"

# The four canonical relaunch routes the force_background_bash hook
# offers when it blocks a heavy foreground Bash. Pinned here so test
# code and tooling can detect drift in the hook's block-message.
RELAUNCH_ROUTES: tuple[str, ...] = (
    "Bash(..., run_in_background=True)",
    "setsid nohup <cmd> >/tmp/job.log 2>&1 </dev/null &",
    "Task / Agent(..., run_in_background=True)",
    "timeout 7 <cmd>",
)

# Operator's exact wording the hook quotes — pinned so a future
# rewrite that drops it surfaces as a test failure.
OPERATOR_WORDING_MARKER: str = "作業中断はしてほしくない"

# The escape-hatch env var the hook honours.
ESCAPE_HATCH_ENV: str = "CC_ALLOW_FOREGROUND_HEAVY"
