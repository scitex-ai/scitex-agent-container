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

import hashlib
import logging
import shutil
from collections.abc import Iterable
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


def _copy_force_overwrite(src: Path, dst: Path) -> None:
    """``shutil.copy2`` that can overwrite a read-only destination.

    ``copy2`` opens ``dst`` for writing and raises ``PermissionError`` when it
    already exists read-only — and it copies the source mode back afterwards,
    so a 0444 source leaves a 0444 destination that makes the NEXT deploy fail.
    A stale read-only command file thus aborts the deploy and leaves the agent
    DOWN on restart (observed 2026-07-01: a 0444 ``update-docs.md``). Grant
    owner-write before the copy (so it can overwrite) and after (so the next
    deploy can too).

    Same-file skip: when ``dst`` already resolves to ``src`` (a prior "linked
    host file" symlink, a hardlink, or a bind), the copy is a no-op — and
    ``copy2`` raises ``SameFileError`` while ``chmod`` would FOLLOW the link and
    mutate the shared host source. Skip cleanly. (INCIDENT 2026-07-02:
    ``sac agents start/restart neurovista`` on ``~/.claude/commands/autonomous.md``,
    whose dst was a symlink back to the host source.)
    """
    try:
        if dst.exists() and src.samefile(dst):
            return
    except OSError:  # stx-allow: fallback (unreadable/broken dst → proceed with the copy)
        pass
    if dst.exists():
        dst.chmod(dst.stat().st_mode | 0o200)
    shutil.copy2(src, dst)
    dst.chmod(dst.stat().st_mode | 0o200)


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
        _copy_force_overwrite(src, dst)
        logger.info("to_home: host command %s -> %s", src.name, dst)


# --- Launch-time snapshot drift ----------------------------------------------


def _hash_lines(path: Path) -> tuple[str, int]:
    """Return (sha256-8, line count) of ``path`` (read-only).

    The 8-hex prefix is the file identity the card measurement names
    (af83523b / 879b6fff).
    """
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:8]
    count = len(data.decode("utf-8", errors="replace").splitlines())
    return digest, count


def snapshot_drift(pairs: Iterable[tuple[Path, Path]]) -> list[str]:
    """One human line per (snapshot, source) pair that diverges.

    ``snapshot`` is the file the agent will read (the materialized runtime
    home); ``source`` is the dotfiles / host original it should match.
    A pair with an identical sha256 contributes no line; a pair whose
    source is missing gets a line saying so. Pure: reads files, writes
    nothing, logs nothing.

    Line shape (card sac-launch-compares-…-20260905)::

        commands/constitution.md: snapshot 879b6fff (452 lines)
          != source af83523b (449 lines) — restart to pick up
    """
    lines: list[str] = []
    for snapshot, source in pairs:
        label = f"commands/{snapshot.name}"
        snap_hash, snap_count = _hash_lines(snapshot)
        if not source.is_file():
            lines.append(
                f"{label}: snapshot {snap_hash} "
                f"({snap_count} line{'s' if snap_count != 1 else ''}) — "
                "source missing — restart to pick up"
            )
            continue
        src_hash, src_count = _hash_lines(source)
        if src_hash == snap_hash:
            continue
        lines.append(
            f"{label}: snapshot {snap_hash} "
            f"({snap_count} line{'s' if snap_count != 1 else ''}) != "
            f"source {src_hash} ({src_count} "
            f"line{'s' if src_count != 1 else ''}) — restart to pick up"
        )
    return lines


__all__ = [
    "deploy_host_claude_commands",
    "host_claude_commands_dir",
    "snapshot_drift",
]
