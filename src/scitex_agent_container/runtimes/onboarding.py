"""Pre-seed ~/.claude.json projects entry to skip per-workspace onboarding.

When Claude Code launches in a workspace it has never seen before, it shows
interactive prompts (theme selection, login method, dev-channels approval).
For fleet agents these prompts block startup and require a human click.

This module pre-populates the ``projects[<workdir>]`` entry in
``~/.claude.json`` with the minimal fields that signal "onboarding complete",
so Claude Code skips the ceremony and reaches the working prompt immediately.

Only the safe, non-secret fields are written:
  - ``hasCompletedProjectOnboarding``: true — suppresses the wizard
  - ``hasTrustDialogAccepted``: true — suppresses trust prompt
  - ``allowedTools``, ``mcpContextUris``, etc.: empty — safe defaults

Token / credential fields are never touched by this module.

See: ywatanabe1989/todo#396
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimal project entry that satisfies Claude Code's onboarding gate.
# Fields with dynamic values (lastCost, lastSessionId, etc.) are omitted —
# Claude Code fills them in after the first real session.
_ONBOARDING_SEED: dict = {
    "allowedTools": [],
    "mcpContextUris": [],
    "mcpServers": {},
    "enabledMcpjsonServers": [],
    "disabledMcpjsonServers": [],
    "hasTrustDialogAccepted": True,
    "projectOnboardingSeenCount": 1,
    "hasClaudeMdExternalIncludesApproved": True,
    "hasClaudeMdExternalIncludesWarningShown": True,
    "hasCompletedProjectOnboarding": True,
    "lastGracefulShutdown": False,
}


def ensure_project_onboarding(
    workdir: str,
    home: Path | None = None,
) -> bool:
    """Ensure ``~/.claude.json`` has a complete projects entry for ``workdir``.

    If the entry already exists and ``hasCompletedProjectOnboarding`` is True,
    this is a no-op. Otherwise the seed fields are merged into the entry
    (existing keys are preserved so we don't clobber live session stats).

    Args:
        workdir: Absolute path to the agent's working directory.
        home: Override for the home directory. Defaults to ``Path.home()``.

    Returns:
        True if the entry was created or updated; False if already complete.

    Never raises — errors are logged and the function returns False so
    callers can continue without crashing.
    """
    home = home or Path.home()
    claude_json_path = home / ".claude.json"

    # Resolve the workdir to a canonical absolute path
    workdir_path = Path(workdir).expanduser()
    if workdir_path.exists():
        workdir_path = workdir_path.resolve()
    workdir_key = str(workdir_path)

    try:
        data: dict = {}
        if claude_json_path.exists():
            try:
                with claude_json_path.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("cannot read %s: %s", claude_json_path, exc)
                return False

        projects: dict = data.setdefault("projects", {})
        entry = projects.get(workdir_key, {})

        if entry.get("hasCompletedProjectOnboarding"):
            logger.debug("onboarding already seeded for %s", workdir_key)
            return False

        # Merge seed into existing entry (preserve any live stats already there)
        for key, value in _ONBOARDING_SEED.items():
            entry.setdefault(key, value)
        # Ensure the critical gate fields are set even if entry had them as False
        entry["hasCompletedProjectOnboarding"] = True
        entry["hasTrustDialogAccepted"] = True
        entry["hasClaudeMdExternalIncludesApproved"] = True

        projects[workdir_key] = entry

        # Write atomically: temp file + rename
        tmp_path = claude_json_path.with_suffix(".json.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, claude_json_path)
        except OSError as exc:
            logger.warning("cannot write %s: %s", claude_json_path, exc)
            tmp_path.unlink(missing_ok=True)
            return False

        logger.info("onboarding seeded for %s", workdir_key)
        return True

    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("ensure_project_onboarding failed for %s: %s", workdir_key, exc)
        return False
