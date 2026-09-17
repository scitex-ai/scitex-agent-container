"""Agent listings lead with the runtime actually serving work."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from scitex_agent_container.cli_pkg._helpers._agent_list import print_agent_list
from scitex_agent_container.cli_pkg._helpers._console import console


def _row() -> dict:
    return {
        "name": "scholar",
        "status": "running",
        "started_at": "-",
        "host_display": "scitex-compute-03",
        "account": "unused-claude-credential",
        "harness": "hermes",
        "engine": "opencode-go-deepseek-v4.1-flash",
        "model": "deepseek-v4.1-flash",
    }


@contextmanager
def _console_width(width: int) -> Iterator[None]:
    before = console.width
    console.width = width
    try:
        yield
    finally:
        console.width = before


def test_default_listing_shows_runtime_selection_not_stored_credential(capsys) -> None:
    # Arrange
    rows = [_row()]

    # Act
    with _console_width(200):
        print_agent_list(None, rows=rows)
    rendered = capsys.readouterr().out

    # Assert
    assert (
        "Harness" in rendered
        and "Engine" in rendered
        and "Model" in rendered
        and "hermes" in rendered
        and "deepseek" in rendered
        and "unused-claude-credential" not in rendered
    )


def test_verbose_listing_labels_account_as_stored_credential(capsys) -> None:
    # Arrange
    rows = [_row()]

    # Act
    with _console_width(200):
        print_agent_list(None, rows=rows, verbose=True)
    rendered = capsys.readouterr().out
    # Assert
    assert "Stored credential" in rendered and "unused-claude-credential" in rendered
