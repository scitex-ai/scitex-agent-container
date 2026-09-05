"""The Codex pane counts as ready after its boot banner has scrolled away."""

from scitex_agent_container.runtimes._pane_acceptance import is_accepting
from scitex_agent_container.runtimes.prompts import is_ready

# Captured from handyman-01 on 2026-09-05 15:45Z (tmux capture-pane), idle.
_IDLE_AFTER_FIRST_TURN = """
\u203a hello?


\u25a0 unexpected status 502 Bad Gateway: No inference upstream answered: All inference
upstreams are cooling down. Configured inference upstreams (2): http://127.0.0.1:18773,
http://127.0.0.1:18774. An upstream that produced no response is out of rotation for 30
s., url: http://127.0.0.1:18772/v1/responses


\u203a Use /skills to list available skills

  qwen38-27b default \u00b7 /home/ywatanabe/proj/local-coder
"""


def test_the_idle_composer_with_the_footer_is_ready_without_the_boot_banner() -> None:
    # Arrange
    pane = _IDLE_AFTER_FIRST_TURN

    # Act
    ready = is_ready(pane)

    # Assert
    assert ready is True


def test_a_confirm_picker_over_the_composer_is_not_ready() -> None:
    # Arrange -- the same screen with a modal footer on it.
    pane = _IDLE_AFTER_FIRST_TURN + "\n  Press enter to confirm \u00b7 Esc to cancel\n"

    # Act
    ready = is_ready(pane)

    # Assert
    assert ready is False


def test_a_working_codex_pane_is_not_accepting() -> None:
    # Arrange -- Codex mid-turn: its working line offers esc to interrupt.
    pane = _IDLE_AFTER_FIRST_TURN.replace(
        "\u203a Use /skills to list available skills",
        "\u2022 Working (12s \u00b7 esc to interrupt)",
    )

    # Act
    accepting = is_accepting(pane)

    # Assert
    assert accepting is False


def test_the_boot_banner_shape_still_counts_as_ready() -> None:
    # Arrange
    pane = "OpenAI Codex (v0.44.0)\n  permissions: YOLO mode\n\n\u203a \n"

    # Act
    ready = is_ready(pane)

    # Assert
    assert ready is True
