"""Diagnose + persist a failed ``runtime.start()`` — the fail-loud boot report.

Extracted from :mod:`._start` (512-line per-file cap; ``_start`` re-exports
:func:`_format_boot_stderr_section` so existing importers are unchanged).
One cohesive responsibility: when ``runtime.start()`` returns a bare ``False``,
turn that into a LOUD, CAUSE-CARRYING error instead of a cause-less
"Failed to start".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["_format_boot_stderr_section", "raise_start_failure"]


def _format_boot_stderr_section(log: Path) -> str:
    """Formatted 'inner stderr' diagnostic section for a failed TUI start.

    B->A feedback / no silent fallback: ``TuiSessionRuntime`` redirects the
    inner ``apptainer exec … claude`` STDERR — where apptainer's FATAL mount
    errors and an immediate claude exit land — to ``<state>/boot.stderr.log``
    (``log``), which SURVIVES the tmux pane's death. Return its tail so a boot
    failure is the LOUD cause in the raised error, never a cause-less
    ``<empty>`` pane fallback. Empty/absent-log safe; never raises.
    """
    tail = ""
    if log.is_file():
        tail = log.read_text(errors="replace")[-4_000:].rstrip()
    body = tail or "<no stderr captured — runtime never launched the process>"
    return f"  inner stderr ({log}):\n{body}\n"


def raise_start_failure(config: Any) -> None:
    """Always raises. Capture WHY the start failed, persist it, then fail loud.

    A bare ``False`` from ``runtime.start()`` must not become a cause-less
    "Failed to start". Capture the agent's tmux pane (the inner
    apptainer/claude output) + whether a session exists, so the real boot
    failure — boot-drain timeout, auth, a broken in-container login shell, an
    immediate claude exit — is visible rather than swallowed.

    The capture is also PERSISTED to ``<state>/start_failure_diag.log``: a
    false-negative start leaves no registry row (we raise before
    ``registry.add()``), so killing the session by hand is often the only way
    to stop the agent — which destroys the live pane capture forever unless it
    is written to disk now. ``boot.stderr.log`` already survives pane death;
    the pane tail did not until this. See
    ``sac-agent-start-false-negative-tui-registry-row``.
    """
    diag = ""
    try:
        from .._runners._tmux.tmux import TmuxManager
        from ..runtimes.tui_session import session_name_for, state_dir_for_config

        _sess = session_name_for(config)
        _pane = TmuxManager.capture_logs(_sess, lines=60).rstrip()
        _boot_log = state_dir_for_config(config) / "boot.stderr.log"
        diag = (
            f" (tmux session_exists={TmuxManager.exists(_sess)})\n"
            f"{_format_boot_stderr_section(_boot_log)}"
            f"  pane tail:\n{_pane or '<empty>'}"
        )
        _diag_log = state_dir_for_config(config) / "start_failure_diag.log"
        _diag_log.write_text(
            f"{datetime.now(timezone.utc).isoformat()} start failed for "
            f"{config.name!r}: runtime.start() returned False.{diag}\n"
        )
    except Exception:  # stx-allow: fallback (reason: diagnostics must never mask the real start failure — degrade to no pane)
        diag = " (no pane diagnostics available)"
    raise RuntimeError(
        f"Failed to start agent '{config.name}': runtime.start() returned "
        f"False.{diag}"
    )
