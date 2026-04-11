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
    """Bypass Permissions mode prompt with radio selector.

    Matches:
      "1. No, exit"
      "2. Yes, I accept"
      "Bypass Permissions"
      "Enter to confirm"
    """
    return (
        "Bypass Permissions" in content
        and "2. Yes, I accept" in content
        and "Enter to confirm" in content
    )


def _detect_dev_channels(content: str) -> bool:
    """Development channels loading confirmation.

    Matches:
      "1. I am using this for local development"
      "2. Exit"
      "development channels" or "dangerously-load-development-channels"
      "Enter to confirm"
    """
    return (
        "1. I am using this for local development" in content
        and "Enter to confirm" in content
    )


def _detect_thinking_effort(content: str) -> bool:
    """Thinking effort level selector.

    Matches:
      "1. * Medium (recommended)" or similar
      "thinking" in various casings
      "Enter to confirm"
    """
    return (
        "Medium" in content
        and ("thinking" in content.lower() or "effort" in content.lower())
        and "Enter to confirm" in content
    )


def _detect_skip_permissions_yn(content: str) -> bool:
    """Legacy y/n text prompt for skip-permissions (older Claude Code).

    Matches text-based y/n prompts without radio selector.
    """
    return (
        ("skip-permissions" in content or "Trust" in content)
        and "Enter to confirm" not in content
        and ("y/n" in content.lower() or "type" in content.lower())
    )


def _detect_done(content: str) -> bool:
    """Check if claude is at the main input prompt (all TUI prompts done).

    The status bar shows "bypass permissions" when ready.
    """
    return "bypass permissions" in content and "Enter to confirm" not in content


# Default prompt handlers — checked by priority, order-agnostic.
# Detection uses numbered options + prompt text for reliability.
# To add a new prompt, append a PromptHandler or call register_prompt().
PROMPT_HANDLERS: list[PromptHandler] = [
    PromptHandler(
        name="bypass-permissions",
        detect=_detect_bypass_permissions,
        keys=["2", "Enter"],  # "2. Yes, I accept"
        priority=1,
    ),
    PromptHandler(
        name="dev-channels",
        detect=_detect_dev_channels,
        keys=["1", "Enter"],  # "1. I am using this for local development"
        priority=2,
    ),
    PromptHandler(
        name="thinking-effort",
        detect=_detect_thinking_effort,
        keys=["1", "Enter"],  # "1. Medium (recommended)"
        priority=3,
    ),
    PromptHandler(
        name="skip-permissions-yn",
        detect=_detect_skip_permissions_yn,
        keys=["y", "Enter"],  # Legacy y/n text prompt
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
