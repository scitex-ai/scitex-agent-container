"""Agent registry -- track running agents via JSON files in a temp directory."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

REGISTRY_DIR = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR",
        os.path.expanduser("~/.scitex/agent-container/registry"),
    )
)


class Registry:
    """File-based registry for tracking running agent instances."""

    def __init__(self, registry_dir: Path | None = None) -> None:
        self.dir = registry_dir or REGISTRY_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def add(
        self,
        name: str,
        config_path: str,
        screen_name: str,
        pid: int | None = None,
    ) -> None:
        """Register an agent as running."""
        data = {
            "name": name,
            "config": config_path,
            "pid": pid or os.getpid(),
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "screen": screen_name,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self._path(name), "w") as f:
            json.dump(data, f, indent=2)

    def remove(self, name: str) -> None:
        """Remove an agent from the registry."""
        path = self._path(name)
        if path.exists():
            path.unlink()

    def get(self, name: str) -> dict | None:
        """Get registry entry for an agent, or None if not found."""
        path = self._path(name)
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def list_all(self) -> list[dict]:
        """List all registered agents."""
        if not self.dir.exists():
            return []
        entries = []
        for path in sorted(self.dir.glob("*.json")):
            # stx-allow: fallback (reason: individual registry entry file may be corrupt or unreadable)
            try:
                with open(path) as f:
                    entries.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                continue
        return entries

    def exists(self, name: str) -> bool:
        """Check if an agent is registered."""
        return self._path(name).exists()

    def cleanup_stale(self) -> int:
        """Remove entries whose multiplexer sessions no longer exist.

        Probes tmux first (tmux has-session), then screen (-ls).  An entry is
        removed only when the session is absent from *both* multiplexers.  This
        makes cleanup safe on mixed fleets where agents may run under either
        tmux or GNU screen.

        Returns count removed.
        """
        import subprocess

        if not self.dir.exists():
            return 0

        def _tmux_alive(session: str) -> bool:
            r = subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True,
            )
            return r.returncode == 0

        def _screen_alive(session: str) -> bool:
            r = subprocess.run(
                ["screen", "-ls", session],
                capture_output=True,
                text=True,
            )
            return session in r.stdout

        cleaned = 0
        for path in list(self.dir.glob("*.json")):
            # stx-allow: fallback (reason: registry entry file may be corrupt; remove it on error)
            try:
                with open(path) as f:
                    data = json.load(f)
                session_name = data.get("screen", "")
                if not session_name:
                    continue
                if not _tmux_alive(session_name) and not _screen_alive(session_name):
                    path.unlink()
                    cleaned += 1
            except (json.JSONDecodeError, OSError):
                path.unlink(missing_ok=True)
                cleaned += 1

        return cleaned
