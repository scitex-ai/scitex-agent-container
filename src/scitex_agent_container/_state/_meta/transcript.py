"""Claude Code transcript / SDK session helpers.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports every helper so existing
``agent_meta._latest_jsonls`` / ``_encode_claude_project`` /
``_read_sdk_session_state`` access keeps working.
"""

from __future__ import annotations

import re
from pathlib import Path


def _encode_claude_project(workdir: str) -> str:
    """Replicate Claude Code's cwd -> projects dir name encoding.

    ``/`` and ``.`` both become ``-``, but triple-or-more dashes that
    come from hidden dirs (``/.foo``) are collapsed back to ``--``.
    """
    encoded = workdir.replace("/", "-").replace(".", "-")
    return re.sub(r"-{3,}", "--", encoded)


def _latest_jsonls(workdir: str) -> list[Path]:
    # Claude Code encodes the *resolved* cwd, so follow symlinks first.
    # stx-allow: fallback (reason: broken symlink or cross-device path can
    # raise — raw workdir string is an acceptable fallback for encoding)
    try:
        resolved = str(Path(workdir).expanduser().resolve())
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        resolved = workdir
    proj_dir = Path.home() / ".claude" / "projects" / _encode_claude_project(resolved)
    if not proj_dir.is_dir():
        return []
    # stx-allow: fallback (reason: concurrent file deletion between glob and
    # stat() causes OSError — return empty list rather than raising)
    try:
        return sorted(
            proj_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return []


def _read_sdk_session_state(name: str, workdir: str) -> dict | None:
    """Surface ``runtime: claude-session`` state on the status JSON.

    Returns ``None`` for agents that aren't using the SDK runtime
    (heartbeat file absent). For SDK agents, returns a dict with the
    persisted session id, accumulated per-turn token totals, and the
    latest heartbeat state. Best-effort: any IO / parse failure
    yields ``None`` so non-SDK agents never see this field populated
    and SDK agents on transient state-dir glitches degrade silently.
    """
    try:
        from ..._runners import claude_session as _runner
    except Exception:  # stx-allow: fallback (reason: import path may differ in tests / partial installs — collect_rich is best-effort)
        return None

    # Try the project-local state root first (matches the runtime
    # adapter's _project_runtime_root logic — keeps the read symmetric
    # with the write path).
    # Walk from cwd, NOT workdir: ``workdir`` may point at a /tmp
    # scratch dir while the agent's YAML lives under a project-scope
    # repo. cwd is what discovery already uses on ``sac agent start``, so
    # the read here stays symmetric with the write.
    try:
        from scitex_config._ecosystem import local_state

        scope = local_state.find_project_scope("agent-container")
    except Exception:  # stx-allow: fallback (reason: scitex-config optional)
        scope = None

    state_dir = (
        (scope / "runtime" / name) if scope is not None else _runner.state_dir_for(name)
    )
    if not (state_dir / "heartbeat.json").is_file():
        return None

    return {
        "session_id": _runner.read_session_id(state_dir),
        "quota": _runner.read_quota(state_dir),
        "heartbeat": _runner.read_heartbeat(state_dir),
        "state_dir": str(state_dir),
    }
