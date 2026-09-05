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
import re
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


# Unicode whitespace Claude's Ink TUI renders where an ASCII space is expected.
# The NBSP part is CAPTURED behaviour, not a guess: every REAL captured pane in
# ``fixtures/pane_states`` shows the ``❯`` prompt gap as U+00A0, and the head-mba
# capture renders the gap after the ``⎿`` result marker as U+00A0 too. The wider
# set is defensive — those variants have NOT been observed from this TUI, they
# are carried over from the proven emacs-claude-code matcher
# (``--ecc-state-detection--normalize-text``) so a future Ink release that
# substitutes one cannot silently blind the watchdog.
_TUI_UNICODE_SPACE_RE = re.compile("[\u00a0\u2000-\u200b\u202f\u205f\u3000]")


def normalize_tui_whitespace(text: str) -> str:
    """Map the Unicode spaces the Ink TUI emits onto ASCII spaces.

    The substitution is 1:1 and LENGTH-PRESERVING — it never collapses runs of
    whitespace — so a caller's ``lstrip`` / ``startswith`` offsets keep meaning
    the same thing they did on the raw line.
    """
    return _TUI_UNICODE_SPACE_RE.sub(" ", text)


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
    lines = [ln for ln in content.splitlines() if ln.strip()]
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
    ``❯[ \\t]+\\S`` (non-whitespace after the prompt marker on the same
    line), meaning the user has typed something but not yet pressed Enter.
    We mirror that pattern here so the prompts system can submit it via
    a plain Enter keystroke.

    Excluded: lines that are just the decorative separator below an empty
    prompt — those contain only whitespace after ``❯``.
    """
    # NBSP (U+00A0) included: Claude's Ink TUI renders the prompt gap as
    # ``❯\xa0[Pasted text …]`` (NBSP, not ASCII space); ``❯[ \t]+`` missed it
    # so a pasted-but-unsent buffer went undetected (proj-scitex-dev 2026-06-23).
    return bool(re.search(r"❯[ \t\xa0]+\S", content))


# The Claude Code Ink TUI input-prompt marker (U+276F). The gap after it may
# be an ASCII space, a tab, or a NBSP (U+00A0) — see
# :func:`_detect_compose_pending_unsent`. Exposed as a named constant so
# prompt-anchored consumers (e.g. the auth-status matcher) locate the input
# line the SAME, NBSP-aware way instead of re-deriving the glyph.
PROMPT_MARKER = "❯"  # ❯


def prompt_line_index(content: str) -> int | None:
    """Return the 0-based line index of the TUI input-prompt line, else ``None``.

    The prompt line is the one whose stripped text STARTS with the Ink prompt
    marker ``❯`` (:data:`PROMPT_MARKER`). A captured pane can show several — a
    scrollback echo of an earlier prompt box sits above the live one — so the
    LAST (bottom-most) match wins: the live input field is always the lowest
    prompt on screen. Anchoring on the stripped-line START (not a bare
    substring) means a ``❯`` that appears mid-sentence in agent prose does not
    masquerade as the prompt.
    """
    idx: int | None = None
    for i, line in enumerate(content.splitlines()):
        if line.strip().startswith(PROMPT_MARKER):
            idx = i
    return idx


def _detect_codex_dir_trust(content: str) -> bool:
    """Codex's first-boot directory-trust picker (harness codex, 2026-09-05).

    "Do you trust the contents of this directory? ... 1. Yes, continue /
    2. No, quit / Press enter to continue" — the cursor already sits on
    option 1, so Enter alone accepts. Lower-case "enter" and different
    wording keep it out of every Claude detector above; measured on the
    first live codex pane (handyman-01), where the drain sat at this
    screen until its timeout.
    """
    return (
        "Do you trust the contents of this directory" in content
        and "1. Yes, continue" in content
    )


def _detect_codex_done(content: str) -> bool:
    """Codex is at its input prompt: the banner box is up and no picker remains.

    The Codex TUI never prints Claude's "bypass permissions" status line; its
    ready state is the "OpenAI Codex (vX)" box with the permissions row
    ("YOLO mode" when sac turns the sandbox off) and no pending picker.
    """
    return (
        "OpenAI Codex (v" in content
        and "permissions:" in content
        and "Press enter to continue" not in content
    )


def _detect_done(content: str) -> bool:
    """Check if the TUI is at its main input prompt (all pickers done).

    Claude's status bar shows "bypass permissions" when ready; Codex has its
    own banner (:func:`_detect_codex_done`).
    """
    if "bypass permissions" in content and "Enter to confirm" not in content:
        return True
    return _detect_codex_done(content)


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
        name="codex-dir-trust",
        detect=_detect_codex_dir_trust,
        keys=["Enter"],  # cursor already on "1. Yes, continue"
        priority=1,
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


def _detect_operator_question(content: str) -> bool:
    """The agent is ASKING THE OPERATOR something (AskUserQuestion).

    MEASURED 2026-08-03: scitex-app sat parked on one of these indefinitely.
    Every other detector in this file keys on ``"Enter to confirm"`` (16
    occurrences). This dialog's footer reads ``"Enter to select · ↑/↓ to
    navigate · n to add notes · Esc to cancel"`` -- a string that appears
    NOWHERE in this module, so no handler matched, the watchdog never saw it,
    and the agent waited forever for input nobody was going to give.

    THIS HANDLER DELIBERATELY CARRIES NO KEYS. The other handlers answer SETUP
    dialogs with a known-correct response -- theme, trust, login method. This
    one is the opposite: the agent raised it BECAUSE it needs the operator's
    judgement, and the options are usually not interchangeable. In the observed
    case option 1 was a packaging decision that contradicted a standing
    operator ruling. Auto-selecting would fabricate consent, which is worse
    than hanging: hanging is at least visible once something reports it.

    So the bug being fixed is not "it goes unanswered" -- it is that it goes
    unanswered SILENTLY.
    """
    c = normalize_tui_whitespace(content)
    return (
        "Enter to select" in c
        and ("to navigate" in c or "add notes" in c)
        # Never fire on the known radio dialogs, which all say "confirm".
        and "Enter to confirm" not in c
    )


#: Detect-only, appended after the auto-accept handlers are defined. Priority 0
#: so it is evaluated FIRST: if the agent is genuinely asking the operator
#: something, no lower-priority auto-accepter may reach it and answer on the
#: operator's behalf.
PROMPT_HANDLERS.append(
    PromptHandler(
        name="operator-question",
        detect=_detect_operator_question,
        keys=[],  # INTENTIONALLY EMPTY -- see the docstring above.
        priority=0,
    )
)
PROMPT_HANDLERS.sort(key=lambda h: h.priority)


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
            if not handler.keys:
                # DETECT-ONLY. A handler with no keys is an ESCALATION, not an
                # acceptance: the pane is showing something this watchdog must
                # NOT answer. Returning the name (rather than None) is what
                # stops a lower-priority auto-accepter from reaching it and
                # answering on the operator's behalf -- but nothing is sent,
                # and it must never be logged as "accepted", because that line
                # is how a silently-unanswered prompt would read as handled.
                logger.warning(
                    "OPERATOR INPUT REQUIRED (%s): the pane is showing a "
                    "question addressed to the operator. The watchdog "
                    "deliberately did NOT answer it -- the options are not "
                    "interchangeable and choosing one would fabricate consent. "
                    "This agent is BLOCKED until a human answers. Attach with "
                    "`sac agents attach <name>` (or `tmux attach -t tui-<name>`)"
                    " and answer it.",
                    handler.name,
                )
                return handler.name
            for key in handler.keys:
                send_keys_fn(key)
            logger.info("Auto-accepted prompt: %s", handler.name)
            return handler.name
    return None


def is_ready(content: str) -> bool:
    """Check if claude is at the main input prompt (all TUI prompts done)."""
    return _detect_done(content)
