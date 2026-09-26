"""The codex pickers must live in the registry the BOOT DRAIN reads.

``runtimes/_tui_drain`` imports ``runtimes.prompts``; ``_runners/_tmux/prompts``
is a second, separate registry. #1300/#1301 taught only the latter, so a live
restart sat on Codex's "Hooks need review" picker until its window expired
(handyman-01, 2026-09-05 10:05-10:09 UTC) even though the handler existed.
These tests pin the handlers to the module the drain actually consults.
"""

from __future__ import annotations

from scitex_agent_container.runtimes import _tui_drain
from scitex_agent_container.runtimes.prompts import detect, is_ready

_TRUST = """
> You are in /home/ywatanabe/proj/local-coder
  Do you trust the contents of this directory? Working with untrusted contents
› 1. Yes, continue
  2. No, quit
  Press enter to continue
"""

_HOOKS = """
  Hooks need review
  49 hooks are new or changed.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
  Press enter to confirm or esc to go back
"""

_READY = """
│ >_ OpenAI Codex (v0.147.0)                    │
│ model:       qwen38-27b   /model to change    │
│ permissions: YOLO mode                        │
› Explain this codebase
"""


def test_the_drain_reads_this_registry():
    # Arrange -- the import that made the earlier fix ineffective.
    module = _tui_drain._prompts
    # Act
    name = module.__name__
    # Assert
    assert name.endswith("runtimes.prompts")


def test_the_trust_picker_is_detected_here():
    # Arrange
    content = _TRUST
    # Act
    modal = detect(content)
    # Assert
    assert modal == "codex-dir-trust"


def test_the_hooks_picker_is_detected_here():
    # Arrange
    content = _HOOKS
    # Act
    modal = detect(content)
    # Assert
    assert modal == "codex-hooks-review"


def test_the_hooks_picker_is_not_ready():
    # Arrange -- its footer says "confirm", not "continue".
    content = _HOOKS
    # Act
    ready = is_ready(content)
    # Assert
    assert ready is False


def test_the_codex_banner_is_ready_here():
    # Arrange -- Codex never prints Claude's status line.
    content = _READY
    # Act
    ready = is_ready(content)
    # Assert
    assert ready is True
