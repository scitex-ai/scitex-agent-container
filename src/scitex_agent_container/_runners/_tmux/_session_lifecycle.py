"""tmux runner — multiplexer-lifecycle helpers.

Extracted from ``_runners/_tmux/claude_code.py`` (Day-2 split) so the
orchestrator stays under the 512-LOC discipline cap.

Owns:
* Building the ``claude`` CLI command (`_build_command`).
* Building the env-export prelude (`_build_env_exports`).
* Session-resume probe (`_session_resumable` +
  ``_encode_workdir_for_claude_projects``).
* Workspace materialisation / cleanup glue (CLAUDE.md, .mcp.json,
  settings.json + onboarding) — what the orchestrator runs *around*
  the multiplexer bring-up / teardown.

The orchestrator (``claude_code.py``) calls these as free functions;
keeping them out of the runtime class makes them testable without an
``AgentConfig`` factory.

Day-2 scope (D): the legacy ``src_files`` deploy/cleanup branch and
the ``ssh_remote`` dispatch branch were removed wholesale — the
A2A→tmux bridge supersedes both as the inbound + remote channel.
"""

from __future__ import annotations

import logging
import os as _os
import re
import time
from pathlib import Path

from ...config import AgentConfig
from ...runtimes.claude_md import cleanup_claude_md, setup_claude_md
from ...runtimes.mcp_config import cleanup_mcp_config, setup_mcp_config
from ...runtimes.onboarding import ensure_project_onboarding
from ...runtimes.settings_json import (
    cleanup_settings_json,
    ensure_global_settings_json,
    setup_settings_json,
)

logger = logging.getLogger(__name__)


def _encode_workdir_for_claude_projects(workdir: str) -> str:
    """Encode a workdir path the way Claude Code names its projects dir.

    Claude Code stores per-project session history under
    ``~/.claude/projects/<encoded>/`` where ``<encoded>`` is the absolute
    workdir with both ``/`` and ``.`` replaced by ``-``. Dot-prefixed path
    segments like ``/.dotfiles`` therefore produce a double-dash
    (``/`` + ``.`` → ``-`` + ``-``); runs of three or more dashes are
    collapsed back to ``--`` to match Claude Code's own normalization.
    """
    abs_path = str(
        Path(workdir).expanduser().resolve()
        if Path(workdir).expanduser().exists()
        else Path(workdir).expanduser()
    )
    encoded = abs_path.replace("/", "-").replace(".", "-")
    return re.sub(r"-{3,}", "--", encoded)


def _session_resumable(
    workdir: str,
    user_home: str | None = None,
    max_age_minutes: int | None = None,
) -> bool:
    """Return True iff Claude Code has a resumable session for ``workdir``.

    A session is considered resumable when
    ``~/.claude/projects/<encoded>/`` exists and contains at least one
    non-empty ``*.jsonl`` transcript. Used by the ``continue-or-new``
    session mode to decide whether ``--continue`` is safe to pass.

    If ``max_age_minutes`` is set, the most-recently-modified jsonl must be
    newer than that many minutes; otherwise returns False (treat as stale).
    """
    home = Path(user_home) if user_home else Path.home()
    encoded = _encode_workdir_for_claude_projects(workdir)
    proj_dir = home / ".claude" / "projects" / encoded
    if not proj_dir.is_dir():
        return False
    candidates = []
    for entry in proj_dir.glob("*.jsonl"):
        try:
            st = entry.stat()
            if entry.is_file() and st.st_size > 0:
                candidates.append((st.st_mtime, entry))
        except OSError:  # stx-allow: fallback (reason: file system operation failure)
            continue
    if not candidates:
        return False
    if max_age_minutes is not None:
        newest_mtime = max(mtime for mtime, _ in candidates)
        age_minutes = (time.time() - newest_mtime) / 60
        if age_minutes > max_age_minutes:
            logger.info(
                "session age %.1f min > max_age_minutes=%d for %s, treating as stale",
                age_minutes,
                max_age_minutes,
                workdir,
            )
            return False
    return True


def build_command(config: AgentConfig) -> str:
    """Build the ``claude`` CLI command from config.

    Session modes:
      - ``continue-or-new`` (default): pass ``--continue`` only when a
        prior session exists for the workdir; otherwise launch fresh.
      - ``continue``: always pass ``--continue`` (may fail if no prior
        session — explicit opt-in for callers that want strict resume).
      - ``new``: never pass ``--continue``.
    """
    parts = ["claude"]
    parts.append(f"--model '{config.model}'")

    for flag in config.claude.flags:
        parts.append(flag)

    workdir = config.expanded_workdir
    if not any(workdir in f for f in config.claude.flags):
        parts.append(f"--add-dir '{workdir}'")

    mode = config.claude.session
    max_age = config.claude.continue_max_age_minutes
    if mode == "continue":
        if max_age is not None and not _session_resumable(
            config.expanded_workdir, max_age_minutes=max_age
        ):
            logger.warning(
                "session=continue: session too stale (max_age=%d min) for %s, "
                "launching fresh",
                max_age,
                config.expanded_workdir,
            )
        else:
            parts.append("--continue")
    elif mode == "continue-or-new":
        if _session_resumable(config.expanded_workdir, max_age_minutes=max_age):
            parts.append("--continue")
            logger.info(
                "session=continue-or-new: resumable session found for %s, "
                "passing --continue",
                config.expanded_workdir,
            )
        else:
            logger.warning(
                "session=continue-or-new: no resumable session for %s, launching fresh",
                config.expanded_workdir,
            )
    elif mode == "resume":
        resume_id = config.claude.resume_id.strip()
        if resume_id:
            parts.append(f"--resume '{resume_id}'")
            logger.info(
                "session=resume: passing --resume %s for %s",
                resume_id,
                config.expanded_workdir,
            )
        else:
            logger.warning(
                "session=resume: no resume_id set for %s, falling back to --continue",
                config.expanded_workdir,
            )
            parts.append("--continue")
    # mode == "new" (or any other): no --continue flag

    return " ".join(parts)


def build_env_exports(config: AgentConfig) -> str:
    """Build export statements from env dict + env_files.

    Values support:
    - ``~`` prefix: expanded to ``$HOME``
    - ``${VAR}`` syntax: resolved from ``os.environ`` at launch time
    """

    def _resolve(val: str) -> str:
        if val.startswith("~"):
            val = val.replace("~", "$HOME", 1)
        return re.sub(
            r"\$\{(\w+)\}",
            lambda m: _os.environ.get(m.group(1), m.group(0)),
            val,
        )

    lines: list[str] = []
    # Source .env files first so explicit env: values in YAML override them.
    for env_file in config.env_files:
        if env_file.startswith("/") or env_file.startswith("~"):
            file_path = f'"{env_file}"'
        else:
            file_path = f'"./{env_file}"'
        lines.append(f"if [ -f {file_path} ]; then set -a; . {file_path}; set +a; fi")
    for key, value in config.env.items():
        lines.append(f'export {key}="{_resolve(str(value))}"')
    # stx-allow: fallback (reason: resolve_hostname() can fail on misconfig;
    # leaving SCITEX_OROCHI_MACHINE unset is safe because the sidecar falls
    # back to its own hostname() call)
    try:
        from ...config._host import resolve_hostname

        _canonical = resolve_hostname()
        if _canonical:
            lines.append(f'export SCITEX_OROCHI_MACHINE="{_canonical}"')
            lines.append(f'export SCITEX_AGENT_CONTAINER_HOSTNAME="{_canonical}"')
    except Exception:  # stx-allow: fallback (catch-all safety net)
        pass
    return "\n".join(lines)


def setup_workspace(config: AgentConfig, workdir: str) -> None:
    """Materialise CLAUDE.md / .mcp.json / settings.json before launch.

    Day-2 (D) simplification: the legacy ``src_files`` branch
    (``deploy_src_claude_md`` / ``deploy_src_mcp_json`` /
    ``deploy_src_env``) was removed. ``setup_claude_md`` (the v1 path
    that generates CLAUDE.md from config) is always used now; the A2A
    bridge supersedes the src_files inbound channel.
    """
    ensure_project_onboarding(workdir)
    setup_claude_md(config, workdir)
    setup_mcp_config(config, workdir)
    ensure_global_settings_json()
    setup_settings_json(config, workdir)


def cleanup_workspace(config: AgentConfig, workdir: str) -> None:
    """Reverse of :func:`setup_workspace` — called from ``stop``."""
    cleanup_claude_md(config, workdir)
    cleanup_mcp_config(config, workdir)
    cleanup_settings_json(config, workdir)


def build_env_source_prelude(workdir: str) -> str:
    """Return a single-line shell snippet that sources ``<workdir>/.env``.

    Run BEFORE explicit env exports so YAML env overrides win. Path-based
    token vars (SCITEX_OROCHI_A2A_TOKEN_PATH, etc.) reach the agent
    process this way.
    """
    env_file = Path(workdir) / ".env"
    return f"if [ -f '{env_file}' ]; then set -a; source '{env_file}'; set +a; fi"


def needs_auto_accept(config: AgentConfig) -> bool:
    """True iff the claude command includes flags that trigger TUI prompts."""
    if not config.claude.auto_accept:
        return False
    dangerous_flags = [
        "--dangerously-skip-permissions",
        "--dangerously-load-development-channels",
    ]
    return any(any(df in f for df in dangerous_flags) for f in config.claude.flags)


__all__ = [
    "_encode_workdir_for_claude_projects",
    "_session_resumable",
    "build_command",
    "build_env_exports",
    "build_env_source_prelude",
    "cleanup_workspace",
    "needs_auto_accept",
    "setup_workspace",
]
