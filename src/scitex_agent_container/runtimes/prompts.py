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


def has_esc_cancel_modal(content: str) -> bool:
    """True iff an on-screen modal treats **Esc as CANCEL / EXIT**.

    The fatal boot bug (card
    ``sac-boot-automation-devchannels-modal-continue-compose-buffer``): the
    stale-compose clear sends ``Escape``, but while the ``--dangerously-load-
    development-channels`` confirmation is up ("❯ 1. I am using this for local
    development … Enter to confirm · **Esc to cancel**") an ``Escape`` CANCELS
    the launch → claude exits → the tmux session DIES mid-boot. Any drain step
    that would send ``Escape`` (the compose-buffer clear) MUST first verify no
    such cancelable modal is on screen.

    Detection is deliberately BROAD — a keystroke that kills the session is far
    costlier than a spurious "don't-Esc" skip:

      * the explicit ``Esc to cancel`` footer any confirm-modal renders, OR
      * the dev-channels modal specifically (its footer wording has varied
        across Claude Code builds; match the option text too so a reworded
        footer still guards).

    Pure + string-only so the drain-ordering guard is unit-testable without a
    live TUI.
    """
    lowered = content.lower()
    if "esc to cancel" in lowered or "escape to cancel" in lowered:
        return True
    return _detect_dev_channels(content)


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
    lines = [line for line in content.splitlines() if line.strip()]
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


def _detect_external_imports(content: str) -> bool:
    """External CLAUDE.md file imports prompt.

    Appears when ``CLAUDE.md`` (or ``.claude/CLAUDE.md``) contains
    ``@<absolute-path>`` imports pointing OUTSIDE the agent's
    workdir. Triggered by the at-import skill-injection mode (sac
    PR #74) when skills live in ``~/.claude/skills/`` or the
    package source trees rather than the workspace itself.

    Matches:
      "Allow external CLAUDE.md file imports?"
      "1. Yes, allow external imports"
      "Enter to confirm"
    """
    return (
        "Allow external CLAUDE.md file imports" in content
        and "1. Yes, allow external imports" in content
        and "Enter to confirm" in content
    )


def _detect_login_method(content: str) -> bool:
    """First-run login-method picker on a fresh HOME.

    Appears when Claude Code can't find OAuth credentials at
    ``~/.claude/.credentials.json``. Even with ``ANTHROPIC_API_KEY``
    set in env, the 2.1.x CLI still asks which auth mode to use
    before it checks the env var. Blocks startup until dismissed.

    Matches the exact option strings to avoid false positives on any
    user message that happens to say "login method".
    """
    return (
        "Select login method:" in content
        and "Claude account with subscription" in content
        and "Anthropic Console account" in content
    )


def _detect_theme_selection(content: str) -> bool:
    """First-run theme selection prompt.

    Appears only on a fresh HOME (no ``~/.claude/`` saved theme). On
    dev machines it never shows, but in CI (a clean ubuntu VM) this is
    the first thing Claude Code asks. Blocks every downstream startup
    prompt until acknowledged.

    Matches the radio variant: "Choose the text style..." + numbered
    options starting with "1. Auto (match terminal)".
    """
    return "Choose the text style" in content and "1. Auto (match terminal)" in content


def _detect_compose_pending_unsent(content: str) -> bool:
    """Detect unsent text sitting in the Claude Code compose buffer.

    The classifier in ``agent_meta._classify_pane_state`` matches
    ``❯[ \\t\\xa0]+\\S`` (non-whitespace after the prompt marker on the same
    line), meaning the user has typed something but not yet pressed Enter.
    We mirror that pattern here so the prompts system can submit it via
    a plain Enter keystroke.

    The gap MUST include U+00A0 NO-BREAK SPACE: Claude Code's Ink TUI renders
    the prompt as ``❯\\xa0[Pasted text …]`` (an NBSP, not an ASCII space). The
    earlier ``❯[ \\t]+`` pattern silently missed it, so a multi-line
    startup_prompt paste was never detected as pending and the boot-drain /
    ``_verify_submitted`` resend never fired — the agent sat idle with its
    instructions pasted-but-unsent (proj-scitex-dev 2026-06-23).

    Excluded: lines that are just the decorative separator below an empty
    prompt — those contain only whitespace after ``❯``.
    """
    import re

    return bool(re.search(r"❯[ \t\xa0]+\S", content))


def _detect_resume_session(content: str) -> bool:
    """Long-session resume picker shown by ``claude --continue`` / ``--resume``.

    When the session being resumed is large, Claude Code interposes a
    three-way picker before the REPL opens::

        This session is <N>h <M>m old and <K>k tokens.
        Resuming the full session will consume a substantial portion ...
        ❯ 1. Resume from summary (recommended)
          2. Resume full session as-is
          3. Don't ask me again

    The sac TUI boot uses ``--continue`` precisely to resume the FULL prior
    context, so we always pick option 2 ("Resume full session as-is"). Left
    unhandled this modal BLOCKS boot: the startup_prompt paste lands inside
    the picker and the boot-drain hangs (lead-retirement, 2026-06-25). Must
    out-rank ``compose-pending-unsent`` — the modal's ``❯ 1.`` line also
    matches that detector — hence priority 1.
    """
    return (
        "Resume full session as-is" in content
        and "Resume from summary" in content
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
        name="resume-session",
        detect=_detect_resume_session,
        keys=[
            "2",
            "Enter",
        ],  # "2. Resume full session as-is" (--continue wants full context)
        priority=1,  # before compose-pending-unsent: the modal's "❯ 1." matches that too
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
    PromptHandler(
        name="theme-selection",
        detect=_detect_theme_selection,
        keys=["1", "Enter"],  # "1. Auto (match terminal)"
        priority=9,
    ),
    PromptHandler(
        name="login-method",
        detect=_detect_login_method,
        keys=["2", "Enter"],  # "2. Anthropic Console account · API usage billing"
        priority=10,
    ),
    PromptHandler(
        name="compose-pending-unsent",
        detect=_detect_compose_pending_unsent,
        keys=["Enter"],  # submit unsent compose buffer
        priority=11,
    ),
    PromptHandler(
        name="external-imports",
        detect=_detect_external_imports,
        keys=["1", "Enter"],  # "1. Yes, allow external imports"
        priority=12,
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


def detect(content: str) -> str | None:
    """Return the NAME of the first matching prompt handler (by priority), or
    None when no known modal is on screen. Detect-only — sends nothing.

    Pairs with :func:`respond_modal` so a drain loop can RESPOND, settle, then
    re-detect to VERIFY the modal actually cleared — instead of firing
    keystrokes once and assuming success. Claude's Ink TUI drops keys sent
    mid-render, so a fire-and-forget send silently leaves the modal up; the
    detect/respond/verify cycle is the no-silent-fallback fix.
    """
    for handler in sorted(PROMPT_HANDLERS, key=lambda h: h.priority):
        if handler.detect(content):
            return handler.name
    return None


def respond_modal(name: str, send_keys_fn: Callable[..., None]) -> bool:
    """Send the registered keystrokes for the handler named ``name``.

    Returns True iff a handler with that name exists (its keys were sent),
    False otherwise. The caller MUST verify the modal cleared (re-capture +
    :func:`detect`) and resend on the render race — a single send is not
    guaranteed to land.
    """
    for handler in PROMPT_HANDLERS:
        if handler.name == name:
            for key in handler.keys:
                send_keys_fn(key)
            logger.info("Responded to prompt %s (keys=%s)", name, handler.keys)
            return True
    return False


def is_ready(content: str) -> bool:
    """Check if claude is at the main input prompt (all TUI prompts done)."""
    return _detect_done(content)
