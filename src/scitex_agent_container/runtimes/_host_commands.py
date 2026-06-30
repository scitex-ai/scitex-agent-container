"""Deploy the operator's host ``~/.claude/commands/*.md`` as an agent baseline.

Claude Code slash commands live in the standard host directory
``~/.claude/commands/`` (one ``<name>.md`` per command). They are NOT
visible inside an agent container, whose ``~/.claude/commands/`` only
carries whatever ``to_home`` materialization deploys. So an operator who
authors ``/incidence`` etc. on the host sees nothing when running it in an
agent.

This module closes that gap: at deploy time it resolves the *host* home's
``~/.claude/commands/`` (``Path("~/.claude/commands").expanduser()`` — never
a hard-coded username) and copies each ``*.md`` into the agent's
materialized ``.claude/commands/``. The operator authors a command ONCE in
the standard host location and it propagates to every agent.

Precedence: host commands are the LOWEST baseline. The materialization
calls :func:`deploy_host_claude_commands` BEFORE the shared-baseline /
per-agent ``to_home`` walk, so any per-agent ``to_home/.claude/commands/<x>.md``
or sac-bundled command of the same name OVERWRITES the host one — matching
the existing to_home cascade precedence (lower layer first, higher layer
wins on conflict).

Skip-if-missing: no host ``~/.claude/commands/`` dir → no-op (no error, no
empty ``commands/`` dir fabricated). Non-``*.md`` entries (e.g.
``archive-*.tar.gz``) are skipped.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# The standard Claude Code slash-commands dir under the operator's host home.
# Resolved fresh at deploy time so it always tracks the real host user (no
# hard-coded username).
_HOST_COMMANDS_REL = "~/.claude/commands"


def host_claude_commands_dir() -> Path | None:
    """Resolve the host ``~/.claude/commands/`` dir, or ``None`` if absent.

    Returns ``None`` (skip-if-missing) when the directory does not exist, so
    callers can no-op without fabricating an empty ``commands/`` dir in the
    agent home.
    """
    p = Path(_HOST_COMMANDS_REL).expanduser()
    return p if p.is_dir() else None


def deploy_host_claude_commands(workspace_home: Path) -> None:
    """Copy host ``~/.claude/commands/*.md`` into ``<workspace_home>/.claude/commands/``.

    Baseline layer: deploy these FIRST (before the shared/per-agent
    ``to_home`` walk) so a same-name per-agent or bundled command wins.

    Skip-if-missing: no host commands dir → no-op. Only ``*.md`` files are
    copied (non-``.md`` entries such as ``archive-*.tar.gz`` are skipped).
    The agent ``commands/`` dir is created only when at least one host
    command is deployed.
    """
    src_dir = host_claude_commands_dir()
    if src_dir is None:
        return
    dst_dir = Path(workspace_home) / ".claude" / "commands"
    for src in sorted(src_dir.iterdir()):
        if not src.is_file() or src.suffix != ".md":
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        shutil.copy2(src, dst)
        logger.info("to_home: host command %s -> %s", src.name, dst)


__all__ = ["deploy_host_claude_commands", "host_claude_commands_dir"]
