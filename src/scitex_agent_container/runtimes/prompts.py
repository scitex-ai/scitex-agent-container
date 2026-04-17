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


def _detect_mcp_json_edit(content: str) -> bool:
    """Permission prompt when Claude tries to edit .mcp.json (runtime).

    Matches "1. Yes" / "1. Proceed" / "1. Allow" + ".mcp.json" + "Enter to confirm".
    """
    return (
        ".mcp.json" in content
        and "Enter to confirm" in content
        and ("1. Yes" in content or "1. Proceed" in content or "1. Allow" in content)
    )


def _detect_press_enter_continue(content: str) -> bool:
    """Generic 'Press Enter to continue' runtime pause (context-window warning, etc).

    Uses a strict last-5-lines window to avoid scrollback false positives
    (per pane-state-patterns.md: classify against last 5 visible lines only).
    Excluded: active tool calls and numbered radio selectors.
    """
    lines = [l for l in content.splitlines() if l.strip()]
    last = "\n".join(lines[-5:]) if lines else ""
    has_enter_cue = (
        "Press Enter to continue" in last
        or "press Enter" in last
        or "Hit Enter" in last
    )
    is_active = "Working\u2026" in last or "Ruminating\u2026" in last
    has_radio = "Enter to confirm" in last or "1. " in last
    return has_enter_cue and not is_active and not has_radio


def _detect_file_trust(content: str) -> bool:
    """'Do you trust the files in this folder?' prompt (first-run or new cwd).

    May appear when --dangerously-skip-permissions was not propagated to a subshell.
    Matches the LEGACY y/n text variant; the new radio-selector variant
    is handled by :func:`_detect_file_trust_radio`.
    """
    return (
        "trust" in content.lower()
        and "folder" in content.lower()
        and ("y/n" in content.lower() or "yes" in content.lower())
        and "Enter to confirm" not in content
    )


def _detect_file_trust_radio(content: str) -> bool:
    """Radio-selector variant of the file-trust prompt.

    Claude Code (>= ~2.1.x) asks "Is this a project you created or one
    you trust?" with numbered options instead of the legacy y/n text
    prompt. Appears on the first launch in any un-trusted workdir —
    including every throwaway tempdir the Haiku integration test uses.

    Matches the exact option strings to avoid firing on the
    bypass-permissions dialog (which also says "Enter to confirm").
    """
    return (
        "1. Yes, I trust this folder" in content
        and "2. No, exit" in content
        and "Enter to confirm" in content
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
        name="mcp-json-edit",
        detect=_detect_mcp_json_edit,
        keys=["1", "Enter"],  # "1. Yes, proceed" — .mcp.json edit dialog
        priority=4,
    ),
    PromptHandler(
        name="skip-permissions-yn",
        detect=_detect_skip_permissions_yn,
        keys=["y", "Enter"],  # Legacy y/n text prompt
        priority=5,
    ),
    PromptHandler(
        name="press-enter-continue",
        detect=_detect_press_enter_continue,
        keys=["Enter"],  # Dismiss informational banners / context-window warnings
        priority=6,
    ),
    PromptHandler(
        name="file-trust",
        detect=_detect_file_trust,
        keys=["y", "Enter"],  # "Do you trust the files in this folder?"
        priority=7,
    ),
    PromptHandler(
        name="file-trust-radio",
        detect=_detect_file_trust_radio,
        keys=["1", "Enter"],  # "1. Yes, I trust this folder"
        priority=8,
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
