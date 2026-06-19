"""Pre-seed ~/.claude.json to skip Claude Code's first-run onboarding.

When Claude Code launches it gates on two independent onboarding layers:

  * **Global first-run** (top-level ``hasCompletedOnboarding``). On a FRESH
    ``~/.claude.json`` this key is absent, so Claude runs its first-run
    onboarding wizard — which includes the OAuth LOGIN screen — and IGNORES
    an otherwise-valid bound credential. A fresh fleet agent then sits on the
    login prompt forever (verified live: figrecipe↔beta a2a demo, 2026-06-19).
  * **Per-workspace** (``projects[<workdir>].hasCompletedProjectOnboarding``).
    A workspace Claude has never seen shows the theme / trust / dev-channels
    prompts.

For fleet agents BOTH prompts block startup and require a human click. This
module pre-populates both layers so Claude skips the ceremony and reaches the
working prompt immediately on a valid credential.

Only the safe, non-secret fields are written:

  Top-level (global first-run gate — see :data:`_TOP_LEVEL_SEED`):
    - ``hasCompletedOnboarding``: true — suppresses the first-run wizard
      (and its OAuth login screen) so a bound credential is honoured
    - ``theme``: a default — Claude otherwise prompts to pick one
    - ``numStartups``: a small positive int — signals "not a first launch"

  Per-workspace (see :data:`_ONBOARDING_SEED`):
    - ``hasCompletedProjectOnboarding``: true — suppresses the wizard
    - ``hasTrustDialogAccepted``: true — suppresses trust prompt
    - ``allowedTools``, ``mcpContextUris``, etc.: empty — safe defaults

Every seed is idempotent: an existing value is NEVER overwritten (only an
absent or falsy-completion gate flag is forced true). Token / credential
fields are never touched by this module.

See: ywatanabe1989/todo#396 ; fresh-agent boot reliability (2026-06-19).
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

# Top-level keys that gate Claude Code's GLOBAL first-run onboarding (the
# wizard that includes the OAuth login screen). On a fresh ``~/.claude.json``
# these are absent and Claude runs first-run onboarding, ignoring a bound
# credential. Seeding them (idempotently — never clobbering an operator's own
# value) makes a fresh agent boot straight onto its credential.
#   - ``hasCompletedOnboarding``: the headline gate.
#   - ``theme``: Claude prompts for a theme on first run; "dark" is a safe,
#     conventional default (it never overrides an existing choice).
#   - ``numStartups``: Claude treats 0/absent as a first launch; a small
#     positive int signals "already launched" without faking a large count.
_TOP_LEVEL_SEED: dict = {
    "hasCompletedOnboarding": True,
    "theme": "dark",
    "numStartups": 1,
}


def _apply_top_level_seed(data: dict) -> bool:
    """Seed the GLOBAL first-run gate keys into ``data`` (idempotent).

    Sets each :data:`_TOP_LEVEL_SEED` key only when ABSENT, so an operator's
    own ``theme`` / ``numStartups`` is never clobbered. ``hasCompletedOnboarding``
    is additionally forced true when present-but-falsy (a stale ``false`` left
    by a half-finished first run would still re-trigger the OAuth login wizard,
    exactly the failure this guards against).

    Returns True iff ``data`` was mutated (so the caller knows a write is owed).
    """
    changed = False
    for key, value in _TOP_LEVEL_SEED.items():
        if key not in data:
            data[key] = value
            changed = True
    if not data.get("hasCompletedOnboarding"):
        data["hasCompletedOnboarding"] = True
        changed = True
    return changed


def ensure_project_onboarding(
    workdir: str,
    home: Path | None = None,
) -> bool:
    """Ensure ``~/.claude.json`` clears Claude Code's onboarding gates.

    Seeds two independent layers (see the module docstring), both idempotent:

      1. **Global first-run** — top-level ``hasCompletedOnboarding`` (+ ``theme``,
         ``numStartups``) via :func:`_apply_top_level_seed`. Without this a fresh
         agent runs Claude's first-run wizard (OAuth login screen) and ignores a
         valid bound credential. Applied UNCONDITIONALLY, even when the
         per-workspace entry is already complete — a workspace can be trusted
         while the global gate is still missing.
      2. **Per-workspace** — the ``projects[<workdir>]`` entry. A no-op when that
         entry already has ``hasCompletedProjectOnboarding`` true; otherwise the
         seed fields are merged in (existing keys preserved so live session
         stats are not clobbered).

    Args:
        workdir: Absolute path to the agent's working directory.
        home: Override for the home directory. Defaults to ``Path.home()``.

    Returns:
        True if EITHER layer was created or updated; False if both were already
        complete (nothing written).

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

        # Global first-run gate — independent of the per-workspace entry, so a
        # fresh ``~/.claude.json`` that lacks ``hasCompletedOnboarding`` is
        # always fixed (even if the workspace happens to be pre-trusted).
        top_changed = _apply_top_level_seed(data)

        projects: dict = data.setdefault("projects", {})
        entry = projects.get(workdir_key, {})

        project_changed = False
        if entry.get("hasCompletedProjectOnboarding"):
            logger.debug("onboarding already seeded for %s", workdir_key)
        else:
            # Merge seed into existing entry (preserve live stats already there)
            for key, value in _ONBOARDING_SEED.items():
                entry.setdefault(key, value)
            # Force the critical gate fields true even if entry had them False
            entry["hasCompletedProjectOnboarding"] = True
            entry["hasTrustDialogAccepted"] = True
            entry["hasClaudeMdExternalIncludesApproved"] = True
            projects[workdir_key] = entry
            project_changed = True

        if not (top_changed or project_changed):
            return False

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
