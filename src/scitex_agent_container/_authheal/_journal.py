"""The LOG. Every agent examined, every verdict, every command, every byte back.

WHY THIS IS ITS OWN MODULE, AND WHY IT MAY BLOCK A RESTART
    The deployed ``auth-heal.py`` ran ``subprocess.run(..., capture_output=True)``
    and then used only the return code. The stdout and stderr of every restart it
    ever performed were captured and DISCARDED. When those restarts stopped
    working, there was nothing to read — the evidence had been collected and
    thrown away at the moment it was collected. The explanation for a MISSED
    agent went into a state.db cache ``note`` field, which no one tails.

    So logging here is not decoration around the work; it is half of the work.
    The operator's rule is that with logs you can work anything out afterwards,
    and its contrapositive is the design constraint: an action taken with no
    record is an action nobody can ever audit. Hence :meth:`Journal.usable` —
    a pass that cannot open its log REFUSES to restart anything, rather than
    repeating the exact failure this feature exists to end. It still REPORTS
    (a read-only pass loses nothing by being unlogged), and it says so loudly.

FORMAT
    Plain text, one timestamped event per line, sitting beside the existing
    ``auth-heal.log`` in the runtime dir so the operator tails it the same way.
    Multi-line payloads — the raw pane capture, a restart's stdout and stderr —
    follow their event as a verbatim ``    | `` -prefixed block. Nothing is
    truncated, summarised or dropped: the prefix is the only edit, so the
    original is recoverable by stripping it.

    Empty payloads are written as an explicit ``    | (empty)`` marker rather
    than as nothing at all, because a command that printed nothing and a
    command whose output we failed to record must not render identically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "Journal",
    "log_path",
]

#: Explicit override for WHERE this pass's log lives, so a host whose runtime
#: dir is not writable can still be given a durable one.
_LOG_ENV = "SAC_LOGIN_REQUIRED_LOG"

#: Prefix for every verbatim block line. Chosen so the original payload is
#: recoverable with a plain ``sed 's/^    | //'`` and so a blank payload line
#: still occupies a line in the log.
_BLOCK = "    | "

_TAG = "login-required"


def log_path() -> Path:
    """Where the log lives. Resolved PER CALL, never cached at import.

    A module-level constant would bake ``$SCITEX_AGENT_CONTAINER_RUNTIME_DIR``
    at import time, before a test (or a per-agent runtime override) sets it —
    the trap :mod:`.._state.state_paths` documents having already paid for.
    """
    override = os.environ.get(_LOG_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / "login-required-restart.log"


def _stamp(now: float | None = None) -> str:
    when = (
        datetime.now(tz=timezone.utc)
        if now is None
        else datetime.fromtimestamp(now, tz=timezone.utc)
    )
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Journal:
    """An append-only text log that knows whether it is actually working.

    ``usable`` is the question the pass asks before it is allowed to act. It is
    answered by WRITING — the probe is the operation, the only proof that cannot
    be stale (``os.access`` answers from permission bits and is routinely wrong
    about NFS, ACLs and revoked mounts).
    """

    path: Path
    usable: bool = False
    detail: str = ""

    @classmethod
    def open(cls, path: Path | None = None) -> "Journal":
        """Open (and PROVE we can append to) the log. Never raises."""
        target = path if path is not None else log_path()
        journal = cls(path=target)
        # stx-allow: fallback (reason: this IS the writability probe — the raise
        # is the answer, converted into an unusable Journal the pass must refuse
        # to act behind, never swallowed into a silently-nonlogging run)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            journal.detail = (
                f"cannot append to {target} ({exc}) — nothing this pass did "
                f"could be recorded, and an unrecorded restart is one nobody "
                f"can ever audit"
            )
            return journal
        journal.usable = True
        journal.detail = str(target)
        return journal

    def event(self, kind: str, message: str = "", *, now: float | None = None) -> None:
        """Write one timestamped event line."""
        line = f"{_stamp(now)} [{_TAG}] {kind}"
        if message:
            line += f" {message}"
        self._write(line + "\n")

    def block(
        self, kind: str, label: str, payload: str, *, now: float | None = None
    ) -> None:
        """Write an event line, then ``payload`` VERBATIM as an indented block.

        The byte count goes on the event line so a reader can tell a genuinely
        empty payload from a block that was cut short.
        """
        raw = payload or ""
        self.event(kind, f"{label} bytes={len(raw.encode('utf-8'))}", now=now)
        if not raw:
            self._write(f"{_BLOCK}(empty)\n")
            return
        self._write("".join(f"{_BLOCK}{ln}\n" for ln in raw.splitlines()))

    def _write(self, text: str) -> None:
        if not self.usable:
            return
        # stx-allow: fallback (reason: the log can become unwritable mid-pass —
        # a full disk, a revoked mount. Flipping to unusable makes the pass STOP
        # applying restarts at its next check rather than continue unrecorded.)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            self.usable = False
            self.detail = f"log {self.path} became unwritable mid-pass ({exc})"
