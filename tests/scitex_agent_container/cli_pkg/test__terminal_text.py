"""Terminal-bound external values are inert without optional services."""

from __future__ import annotations

import random

import pytest

from scitex_agent_container.cli_pkg._terminal_text import terminal_safe


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


@pytest.mark.parametrize("codepoint", [*range(32), *range(127, 160)])
def test_every_c0_and_c1_control_is_removed(codepoint: int) -> None:
    # Arrange
    value = f"left{chr(codepoint)}right"

    # Act
    rendered = terminal_safe(value)

    # Assert
    assert not _has_control(rendered)


@pytest.mark.parametrize(
    "codepoint",
    [*range(27), *range(28, 32), *range(127, 155), 156, *range(158, 160)],
)
def test_non_introducer_controls_preserve_adjacent_printable_text(
    codepoint: int,
) -> None:
    # Arrange
    value = f"left{chr(codepoint)}right"

    # Act
    rendered = terminal_safe(value)

    # Assert
    assert rendered == "leftright"


@pytest.mark.parametrize(
    "sequence",
    [
        "\x1b]8;;https://evil.invalid\x07click\x1b]8;;\x07",
        "\x1b]8;;https://evil.invalid\x1b\\click\x1b]8;;\x1b\\",
        "\x9d8;;https://evil.invalid\x9cclick\x9d8;;\x9c",
        "\x1b[31mred\x1b[0m",
        "\x9b31mred\x9b0m",
    ],
)
def test_osc_hyperlinks_and_csi_styles_leave_only_printable_payload(
    sequence: str,
) -> None:
    # Arrange
    value = f"before{sequence}after"

    # Act
    rendered = terminal_safe(value)

    # Assert
    assert rendered in {"beforeclickafter", "beforeredafter"} and not _has_control(
        rendered
    )


def test_markup_looking_plain_text_is_preserved_literally() -> None:
    # Arrange
    value = "[/][link=https://evil.invalid]literal[/link]"

    # Act
    rendered = terminal_safe(value)

    # Assert
    assert rendered == value


def test_seeded_control_fuzz_never_emits_escape_or_bell() -> None:
    # Arrange
    randomizer = random.Random(1498)
    alphabet = [chr(codepoint) for codepoint in range(256)]
    values = [
        "".join(randomizer.choice(alphabet) for _ in range(128)) for _ in range(100)
    ]

    # Act
    rendered = [terminal_safe(value) for value in values]

    # Assert
    assert all(
        "\x1b" not in value and "\x07" not in value and not _has_control(value)
        for value in rendered
    )
