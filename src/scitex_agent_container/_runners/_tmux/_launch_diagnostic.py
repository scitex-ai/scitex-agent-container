"""Durable evidence from the host-side ``tmux new-session`` invocation."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess


def persist_tmux_start_result(path: Path, result: CompletedProcess[str]) -> None:
    """Atomically persist the exact tmux exit status and captured streams."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "returncode": result.returncode,
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def format_tmux_start_result(path: Path) -> str:
    """Render a persisted result for the caller-visible start diagnostic."""
    if not path.exists():
        return "\n  tmux new-session: <no invocation record>"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return "\n  tmux new-session: <unreadable invocation record>"
    return (
        f"\n  tmux new-session: rc={value.get('returncode', '?')}"
        f"\n    stdout: {value.get('stdout') or '<empty>'}"
        f"\n    stderr: {value.get('stderr') or '<empty>'}"
    )


__all__ = ["format_tmux_start_result", "persist_tmux_start_result"]
