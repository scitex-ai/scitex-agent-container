"""Loader default injection for the startup command every spec inherits.

Extracted verbatim from :mod:`._loaders` when that orchestrator hit the
per-file line cap (v4 step 6 made the residency field land there). One
cohesive responsibility: the guarded direnv-allow startup command ``load_v3``
injects into every agent. Startup prompts have no implicit default: the
standalone spec is authoritative. ``_loaders`` re-imports every public name here, so existing
consumers keep their ``config._loaders`` import path.
"""

from __future__ import annotations

import re

from ._types import StartupCommand

__all__ = [
    "DEFAULT_DIRENV_ALLOW_COMMAND",
    "_with_default_direnv_allow",
]

# Guarded default startup command APPENDED to EVERY agent's ``startup_commands``
# (operator directive, Telegram 2862 / card
# ``sac-auto-direnv-allow-at-agent-start-guarded-20260717``). It whitelists a
# project's ``.envrc`` with direnv so the project's NON-SECRET environment
# surfaces inside the container — WITHOUT any per-spec hand-editing, and VISIBLE
# in the materialized spec (``AgentConfig.startup_commands``), not buried in the
# launch code the operator explicitly did not want.
#
# GUARDED + FAIL-SOFT + IDEMPOTENT:
#   * ``command -v direnv`` — no-op when direnv is not installed;
#   * ``[ -f "$PWD/.envrc" ]`` — no-op when the workdir has no ``.envrc``;
#   * trailing ``|| true`` — a failed allow NEVER breaks the boot.
#
# ``$PWD`` is the agent workdir AT RUN TIME: the inner ``bash -lc`` wrapper that
# runs ``startup_commands`` inherits apptainer's ``--pwd
# str(Path(config.workdir).expanduser())`` (runtimes/_apptainer_build_argv.py)
# and sac emits NO ``cd`` before the commands, so ``$PWD`` == the workdir. If a
# workdir is not bound in-container ``$PWD`` falls back to ``$HOME``/``/`` where
# the ``-f "$PWD/.envrc"`` guard simply finds no ``.envrc`` and skips — still
# fail-soft. This surfaces ONLY the project's ``.envrc``; sac SECRETS and
# IDENTITY (SCITEX_TODO_AGENT_ID, cct token pool, listen bearer) stay
# sac-DIRECT-injected and are never routed through direnv.
DEFAULT_DIRENV_ALLOW_COMMAND = (
    'command -v direnv >/dev/null 2>&1 && [ -f "$PWD/.envrc" ] '
    '&& direnv allow "$PWD" || true'
)

# Recognises an already-authored ``direnv allow`` in a startup command so the
# default is not duplicated (idempotency; tolerates extra whitespace).
_DIRENV_ALLOW_RE = re.compile(r"\bdirenv\s+allow\b")


def _with_default_direnv_allow(
    commands: list[StartupCommand],
) -> list[StartupCommand]:
    """Append the guarded direnv-allow default unless one is already present.

    Idempotent: a spec whose ``startup_commands`` ALREADY run ``direnv allow``
    (authored explicitly) is returned unchanged — no duplicate. Otherwise the
    guarded, fail-soft :data:`DEFAULT_DIRENV_ALLOW_COMMAND` is APPENDED so it
    runs last, just before the claude runner ``exec``s. Appended (not
    prepended) so an authored ``startup_commands[0]`` keeps its position.
    """
    for cmd in commands:
        if _DIRENV_ALLOW_RE.search(cmd.command or ""):
            return commands
    return [*commands, StartupCommand(command=DEFAULT_DIRENV_ALLOW_COMMAND)]
