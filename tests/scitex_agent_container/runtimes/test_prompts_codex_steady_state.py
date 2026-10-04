"""The Codex pane counts as ready after its boot banner has scrolled away."""

import pytest

from scitex_agent_container._runners._tmux.prompts import is_ready as dispatch_ready
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


@pytest.mark.parametrize("effort", ["max", "ultra", "ultra fast", "xhigh fast"])
@pytest.mark.parametrize("ready", [is_ready, dispatch_ready])
@pytest.mark.parametrize("marker", ["›", "»"])
def test_current_codex_reasoning_and_processing_footer_is_ready(
    effort, ready, marker
) -> None:
    # Arrange
    pane = f"{marker} Inspect this project\n\n  GPT-6.1-Sol {effort} · /home/ywatanabe/proj/lead\n"
    # Act
    state = (ready(pane), is_accepting(pane))
    # Assert
    assert state == (True, True)


def test_working_ultra_fast_codex_is_ready_but_not_accepting() -> None:
    # Arrange
    pane = """
• Working (28s • esc to interrupt)
› Inspect this project
  GPT-6.1-Sol ultra fast · /home/ywatanabe/proj/lead
"""
    # Act
    state = (is_ready(pane), is_accepting(pane))
    # Assert
    assert state == (True, False)


# Redacted from Business's actual 24-row capture on 2026-10-04. The bottom
# composer began 11 rows above the pane bottom. Only row widths/indentation,
# the composer marker, and the already-public model/effort footer survive.
_WRAPPED_COMPOSER_ROWS = (
    "›" + "x" * 73,
    "  " + "x" * 39,
    "  " + "x" * 64,
    "  " + "x" * 66,
    "  " + "x" * 48,
    "",
    "  " + "x" * 8,
    "  " + "x" * 10,
    "",
    ("  GPT-6.1-Sol ultra fast · /" + "x" * 80)[:80],
    " " * 54 + "x" * 25,
)


@pytest.mark.parametrize("ready", [is_ready, dispatch_ready])
@pytest.mark.parametrize("marker", ["›", "»"])
def test_wrapped_codex_composer_matches_observed_eleven_row_shape(
    ready, marker
) -> None:
    # Arrange
    rows = list(_WRAPPED_COMPOSER_ROWS)
    rows[0] = marker + rows[0][1:]
    pane = "\n".join(rows)

    # Act
    state = (ready(pane), is_accepting(pane))

    # Assert
    assert state == (True, True)


@pytest.mark.parametrize(
    "cue", ["• Working (28s • esc to interrupt)", "Press up to edit queued messages"]
)
def test_wrapped_codex_preserves_busy_and_queued_refusal(cue) -> None:
    # Arrange
    pane = cue + "\n" + "\n".join(_WRAPPED_COMPOSER_ROWS)

    # Act
    accepting = is_accepting(pane)

    # Assert
    assert accepting is False


@pytest.mark.parametrize(
    "modal",
    [
        "Trust this folder?\n1. Trust and continue\nPress enter to continue",
        "Hooks need review\n2. Trust all and continue\nPress enter to confirm",
        "Press enter to confirm · Esc to cancel",
    ],
)
def test_wrapped_codex_preserves_review_and_confirm_refusal(modal) -> None:
    # Arrange
    pane = "\n".join(_WRAPPED_COMPOSER_ROWS) + "\n" + modal

    # Act
    state = (is_ready(pane), is_accepting(pane))

    # Assert
    assert state == (False, False)


@pytest.mark.parametrize(
    "footer",
    ["unknown status · /project", "GPT-6.1-Sol ultra fast", "GPT-6.1-Sol · /project"],
)
def test_wrapped_codex_requires_the_existing_footer_contract(footer) -> None:
    # Arrange
    rows = list(_WRAPPED_COMPOSER_ROWS)
    rows[-2] = footer
    pane = "\n".join(rows)

    # Act
    ready = is_ready(pane)

    # Assert
    assert ready is False


def test_wrapped_codex_refuses_unindented_intervening_output() -> None:
    # Arrange
    rows = list(_WRAPPED_COMPOSER_ROWS)
    rows[1] = "unrelated assistant output"
    pane = "\n".join(rows)

    # Act
    ready = is_ready(pane)

    # Assert
    assert ready is False


@pytest.mark.parametrize("marker", ["›", "»"])
def test_wrapped_codex_refuses_an_old_empty_composer(marker) -> None:
    # Arrange
    rows = list(_WRAPPED_COMPOSER_ROWS)
    rows[0] = marker + " "
    pane = "\n".join(rows)

    # Act
    ready = is_ready(pane)

    # Assert
    assert ready is False


@pytest.mark.parametrize("marker", ["›", "»"])
def test_wrapped_codex_refuses_a_composer_outside_the_bounded_window(marker) -> None:
    # Arrange
    pane = "\n".join(
        [
            marker + " stale input",
            *(["  old output"] * 18),
            *_WRAPPED_COMPOSER_ROWS[-2:],
        ]
    )

    # Act
    ready = is_ready(pane)

    # Assert
    assert ready is False
