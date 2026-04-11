"""Modular TUI prompt detection and response for Claude Code.

Each prompt handler defines:
- name: identifier for logging
- detect(content) -> bool: whether this prompt is visible
- respond(send_keys) -> None: keystrokes to accept the prompt
- priority: lower = checked first (default 10)

Add new handlers by appending to PROMPT_HANDLERS or calling register_prompt().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class PromptHandler:
    """A single TUI prompt detector and responder."""

    name: str
    detect: Callable[[str], bool]
    keys: list[str] = field(default_factory=list)
    priority: int = 10


def _detect_bypass_permissions(content: str) -> bool:
    return "Bypass Permissions" in content and "Enter to confirm" in content


def _detect_dev_channels(content: str) -> bool:
    return "development channels" in content and "Enter to confirm" in content


def _detect_thinking_effort(content: str) -> bool:
    return "thinking effort" in content.lower() and "Enter to confirm" in content


def _detect_skip_permissions_yn(content: str) -> bool:
    """Legacy y/n prompt for skip-permissions (older Claude Code versions)."""
    return (
        "skip-permissions" in content or "Trust" in content
    ) and "Enter to confirm" not in content


def _detect_done(content: str) -> bool:
    """Check if all prompts are accepted and claude is at the main prompt."""
    return "bypass permissions" in content and "Enter to confirm" not in content


# Default prompt handlers — order matters (checked by priority)
PROMPT_HANDLERS: list[PromptHandler] = [
    PromptHandler(
        name="bypass-permissions",
        detect=_detect_bypass_permissions,
        keys=["Down", "Enter"],  # Select option 2 "Yes, I accept"
        priority=1,
    ),
    PromptHandler(
        name="dev-channels",
        detect=_detect_dev_channels,
        keys=["Enter"],  # Option 1 already selected
        priority=2,
    ),
    PromptHandler(
        name="thinking-effort",
        detect=_detect_thinking_effort,
        keys=["Enter"],  # Option 1 (Medium) already selected
        priority=3,
    ),
    PromptHandler(
        name="skip-permissions-yn",
        detect=_detect_skip_permissions_yn,
        keys=["y", "Enter"],  # Legacy y/n prompt
        priority=5,
    ),
]


def register_prompt(handler: PromptHandler) -> None:
    """Add a custom prompt handler to the registry."""
    PROMPT_HANDLERS.append(handler)
    PROMPT_HANDLERS.sort(key=lambda h: h.priority)


def detect_and_respond(
    content: str,
    accepted: set[str],
    send_keys_fn: Callable[..., None],
) -> str | None:
    """Check content against all handlers, respond to the first match.

    Args:
        content: Captured pane content.
        accepted: Set of already-accepted prompt names.
        send_keys_fn: Callable to send keystrokes (e.g., mux.send_keys).

    Returns:
        Name of the matched prompt, or None if no match.
    """
    for handler in sorted(PROMPT_HANDLERS, key=lambda h: h.priority):
        if handler.name in accepted:
            continue
        if handler.detect(content):
            for key in handler.keys:
                send_keys_fn(key)
            logger.info("Auto-accepted prompt: %s", handler.name)
            return handler.name
    return None


def is_ready(content: str) -> bool:
    """Check if claude is at the main input prompt (all TUI prompts done)."""
    return _detect_done(content)
