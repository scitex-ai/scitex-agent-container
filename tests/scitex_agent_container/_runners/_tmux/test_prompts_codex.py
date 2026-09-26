"""The Codex pickers the tmux boot drain must recognise (harness codex)."""

from __future__ import annotations

from scitex_agent_container._runners._tmux.prompts import PROMPT_HANDLERS, is_ready

_TRUST_SCREEN = """
  Welcome to Codex, OpenAI's command-line coding agent
> You are in /home/ywatanabe/proj/local-coder
  Do you trust the contents of this directory? Working with untrusted contents
› 1. Yes, continue
  2. No, quit
  Press enter to continue
"""

_READY_SCREEN = """
╭───────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.147.0)                    │
│ model:       qwen38-27b   /model to change    │
│ directory:   /home/ywatanabe/proj/local-coder │
│ permissions: YOLO mode                        │
╰───────────────────────────────────────────────╯
› Explain this codebase
  qwen38-27b default · /home/ywatanabe/proj/local-coder
"""


def _handler(name: str):
    return next(h for h in PROMPT_HANDLERS if h.name == name)


def test_codex_trust_picker_is_detected():
    # Arrange -- the first-boot screen measured on handyman-01.
    handler = _handler("codex-dir-trust")
    # Act
    seen = handler.detect(_TRUST_SCREEN)
    # Assert
    assert seen is True


def test_codex_trust_picker_is_answered_with_enter_alone():
    # Arrange -- the cursor already sits on "1. Yes, continue".
    handler = _handler("codex-dir-trust")
    # Act
    keys = handler.keys
    # Assert
    assert keys == ["Enter"]


def test_codex_trust_screen_is_not_ready():
    # Arrange
    content = _TRUST_SCREEN
    # Act
    ready = is_ready(content)
    # Assert
    assert ready is False


def test_codex_banner_is_ready():
    # Arrange -- no Claude "bypass permissions" line ever appears here.
    content = _READY_SCREEN
    # Act
    ready = is_ready(content)
    # Assert
    assert ready is True


_HOOKS_SCREEN = """
  Hooks need review
  49 hooks are new or changed.
  Hooks can run outside the sandbox after you trust them.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
  Press enter to confirm or esc to go back
"""


def test_codex_hooks_review_picker_is_detected():
    # Arrange -- the screen after the shim copied the fleet's hooks in.
    handler = _handler("codex-hooks-review")
    # Act
    seen = handler.detect(_HOOKS_SCREEN)
    # Assert
    assert seen is True


def test_codex_hooks_review_picker_trusts_all():
    # Arrange -- option 3 would run the agent without its hooks, silently.
    handler = _handler("codex-hooks-review")
    # Act
    keys = handler.keys
    # Assert
    assert keys == ["2", "Enter"]


def test_codex_hooks_review_screen_is_not_ready():
    # Arrange
    content = _HOOKS_SCREEN
    # Act
    ready = is_ready(content)
    # Assert
    assert ready is False
