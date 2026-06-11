"""tmux runner — auto-accept polling loop.

Extracted from ``_runners/_tmux/claude_code.py`` (Day-2 split, D) so
the orchestrator stays under the 512-LOC discipline cap.

Owns the loop that polls ``tmux capture-pane`` for the first-run TUI
gauntlet (theme picker, login method, file-trust, bypass-permissions,
dev-channels, …) and replies with the right keystrokes via the
multiplexer's ``send_keys`` until the pane reports a ready state
(``is_ready``).

Diagnostics file:
    ~/.scitex/agent-container/logs/<agent>/auto-accept.log
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from ...config import AgentConfig
from .prompts import PROMPT_HANDLERS, detect_and_respond, is_ready

logger = logging.getLogger(__name__)


def _setup_auto_accept_log(name: str) -> logging.Logger:
    """Create a file logger for auto-accept diagnostics."""
    log_dir = Path.home() / ".scitex" / "agent-container" / "logs" / name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "auto-accept.log"

    file_logger = logging.getLogger(f"auto-accept.{name}")
    file_logger.setLevel(logging.DEBUG)
    file_logger.handlers.clear()
    handler = logging.FileHandler(str(log_file), mode="a")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    file_logger.addHandler(handler)
    file_logger.info(
        "=== Auto-accept session started at %s ===",
        datetime.now().isoformat(),
    )
    return file_logger


def send_auto_accept_keystrokes(
    config: AgentConfig,
    mux,
    timeout: int = 90,
) -> bool:
    """Poll the pane and auto-accept TUI prompts.

    Returns True if all prompts were accepted (the pane reports ready),
    False on timeout.
    """
    flog = _setup_auto_accept_log(config.name)
    handler_names = [h.name for h in PROMPT_HANDLERS]
    logger.info(
        "Auto-accepting TUI prompts for %s (handlers: %s)",
        config.screen_name,
        ", ".join(handler_names),
    )
    flog.info("Handlers: %s", ", ".join(handler_names))

    start = time.monotonic()
    accepted: set[str] = set()
    poll_count = 0
    content_preview = "(not yet polled)"

    def _send(session_name: str, *keys: str) -> None:
        mux.send_keys(session_name, *keys)

    while time.monotonic() - start < timeout:
        poll_count += 1
        elapsed = time.monotonic() - start

        if not mux.exists(config.screen_name):
            msg = (
                f"Session {config.screen_name} disappeared at poll "
                f"{poll_count} ({elapsed:.0f}s)"
            )
            logger.warning(msg)
            flog.warning(msg)
            return False

        content = mux.capture_content(config.screen_name)
        content_preview = content.strip()[:300] if content.strip() else "(empty)"
        flog.debug(
            "Poll %d (%.0fs) accepted=%s content:\n%s",
            poll_count,
            elapsed,
            accepted or "none",
            content_preview,
        )

        if is_ready(content):
            msg = (
                f"Auto-accept complete for {config.screen_name} "
                f"(accepted: {accepted or 'none'}) after {elapsed:.0f}s"
            )
            logger.info(msg)
            flog.info(msg)
            return True

        matched = detect_and_respond(
            content,
            accepted,
            lambda *keys: _send(config.screen_name, *keys),
        )
        if matched:
            accepted.add(matched)
            flog.info(
                "Matched handler '%s' at poll %d (%.0fs), sent keys",
                matched,
                poll_count,
                elapsed,
            )
            time.sleep(2)
            continue

        time.sleep(2)

    msg = (
        f"TIMEOUT ({timeout}s) for {config.screen_name} "
        f"after {poll_count} polls. accepted={accepted or 'none'}. "
        f"Last content:\n{content_preview}"
    )
    logger.warning(msg)
    flog.warning(msg)
    return False


__all__ = ["send_auto_accept_keystrokes"]
