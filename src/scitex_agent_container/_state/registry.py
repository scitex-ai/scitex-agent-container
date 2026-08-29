"""Agent registry -- track running agents via JSON files in a temp directory."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .._runtime_paths import runtime_base_dir

# ``SCITEX_AGENT_CONTAINER_REGISTRY_DIR`` still wins (explicit override);
# its FALLBACK routes through ``runtime_base_dir`` so the single
# ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` knob relocates the registry too.
# Unset env => identical to ``~/.scitex/agent-container/runtime/registry``.
REGISTRY_DIR = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR",
        str(runtime_base_dir() / "registry"),
    )
)


def _default_session_probe(session: str) -> bool | None:
    """Is ``session`` alive? ``True`` / ``False`` / ``None`` when UNKNOWABLE.

    THREE-VALUED ON PURPOSE. The previous implementation returned a bare bool
    and treated every failure as "dead", so a host without ``tmux`` on PATH
    reported every agent gone — ``FileNotFoundError`` is an ``OSError``, and
    the caller unlinked on ``OSError``. A probe that cannot run must say so,
    not vote for deletion.

    ``True`` from either multiplexer wins. ``False`` requires a probe that
    ACTUALLY RAN and reported absence. If neither could run, the answer is
    ``None`` and the caller keeps the entry.
    """
    import subprocess

    from .._runners._tmux._target import exact_target

    verdicts: list[bool] = []
    # stx-allow: fallback (reason: a missing/failing multiplexer binary is
    # UNKNOWN, never evidence of death -- see the docstring incident.)
    try:
        # EXACT target: a bare -t prefix-matches, so a dead agent could be
        # vouched alive by a sibling session (incident 2026-08-14).
        rc = subprocess.run(
            ["tmux", "has-session", "-t", exact_target(session)],
            capture_output=True,
        ).returncode
        verdicts.append(rc == 0)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass
    try:
        out = subprocess.run(
            ["screen", "-ls", session], capture_output=True, text=True
        ).stdout
        verdicts.append(session in out)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass

    if not verdicts:
        return None
    return True if any(verdicts) else False


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
            try:
                with open(path) as f:
                    entries.append(json.load(f))
            except (
                json.JSONDecodeError,
                OSError,
            ):  # stx-allow: fallback (reason: malformed JSON tolerated)
                continue
        return entries

    def exists(self, name: str) -> bool:
        """Check if an agent is registered."""
        return self._path(name).exists()

    def session_candidates(self, data: dict) -> list[str]:
        """Every session name this entry could legitimately be running under.

        The stored ``screen`` field holds the BARE agent name, but the runtime
        creates ``tui-<name>`` (``runtimes.tui_session.session_name_for``, the
        SSOT). Probing the stored value alone therefore misses EVERY live
        agent — measured 2026-08-02 on the live host:

            tmux has-session -t scitex-dev      -> not found
            tmux has-session -t tui-scitex-dev  -> ALIVE

        So resolve through the SSOT and keep the stored value as well, because
        entries written by older sac versions carry only the bare name. The
        prefix is NOT hardcoded here: hardcoding ``tui-`` would be the same
        brittleness pointing the other way, and would break again the next time
        the runtime renames its sessions.
        """
        names: list[str] = []
        # stx-allow: fallback (reason: a spec that no longer loads, or an entry
        # written before `config` was recorded, must not make the agent
        # UNPROBEABLE -- those are exactly the stale-looking entries most at
        # risk of being swept while alive.)
        try:
            from ..config import AgentConfig, load_config
            from ..runtimes.tui_session import session_name_for

            config_path = str(data.get("config") or "")
            config = None
            if config_path:
                try:
                    config = load_config(config_path)
                except Exception:  # stx-allow: fallback (reason: see above)
                    config = None
            if config is None and data.get("name"):
                # Ask the SSOT what it WOULD name this agent's session. Still
                # no hardcoded prefix here -- session_name_for owns the format,
                # so a future rename moves this with it.
                config = AgentConfig(name=str(data["name"]))
            if config is not None:
                names.append(session_name_for(config))
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            pass
        stored = str(data.get("screen") or "")
        if stored and stored not in names:
            names.append(stored)
        return names

    def cleanup_stale(self, *, probe=None) -> int:
        """Remove entries whose multiplexer sessions are POSITIVELY gone.

        Three-valued by design. Each probe returns True (alive), False (this
        multiplexer does not have it) or None (could not tell — the binary is
        missing, or it errored). An entry is unlinked ONLY when every candidate
        session was positively reported absent by a probe that actually ran.

        An UNKNOWN never deletes. That is the whole point: this sweep used to
        convert "I could not tell" into "it does not exist", and every consumer
        downstream — a2a_peers, `sac agents list`, agent_health — inherited that
        as fact. It cost scitex-cards ~6 messages to an agent that was never
        unreachable, because they followed the documented procedure and the
        procedure read a record this sweep had removed.

        ``probe`` is the injection seam: ``(session_name) -> bool | None``.

        Returns count removed.
        """
        if not self.dir.exists():
            return 0

        probe_fn = probe or _default_session_probe

        cleaned = 0
        for path in list(self.dir.glob("*.json")):
            # stx-allow: fallback (reason: an unreadable entry is UNKNOWN, not
            # dead. The old code unlinked on OSError -- which meant a missing
            # tmux binary (FileNotFoundError IS an OSError) deleted the whole
            # registry. Skip and leave it for a human.)
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception:  # stx-allow: fallback (reason: see inline comment)
                continue

            names = self.session_candidates(data)
            if not names:
                continue  # nothing to probe -> UNKNOWN -> keep

            verdicts = [probe_fn(n) for n in names]
            if any(v is True for v in verdicts):
                continue  # alive under at least one name
            if not any(v is False for v in verdicts):
                continue  # no probe positively said "gone" -> UNKNOWN -> keep

            path.unlink(missing_ok=True)
            cleaned += 1

        return cleaned
