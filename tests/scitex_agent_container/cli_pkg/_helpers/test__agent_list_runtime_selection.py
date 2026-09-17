"""Agent listings lead with the runtime actually serving work."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from scitex_agent_container.cli_pkg._helpers._agent_list import print_agent_list
from scitex_agent_container.cli_pkg._helpers._agent_list_render import (
    _narrow_detail_lines,
)
from scitex_agent_container.cli_pkg._helpers._console import console


def _row() -> dict:
    return {
        "name": "scholar",
        "status": "running",
        "started_at": "-",
        "host_display": "scitex-compute-03",
        "account": "unused-claude-credential",
        "billing_mode": "usage",
        "auth_identity": "api-key:OPENCODE_API_KEY",
        "runtime_identity_source": "birth_certificate",
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


def test_default_listing_shows_billing_auth_and_runtime_not_stored_credential(
    capsys,
) -> None:
    # Arrange
    rows = [_row()]

    # Act
    with _console_width(200):
        print_agent_list(None, rows=rows)
    rendered = capsys.readouterr().out

    # Assert
    assert (
        "Harness" in rendered
        and "Billing" in rendered
        and "Auth identity" in rendered
        and "Engine" in rendered
        and "Model" in rendered
        and "hermes" in rendered
        and "usage" in rendered
        and "api-key:OPENCODE_API_KEY" in rendered
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


def test_verbose_listing_shows_runtime_identity_provenance(capsys) -> None:
    # Arrange
    rows = [_row()]

    # Act
    with _console_width(240):
        print_agent_list(None, rows=rows, verbose=True)
    rendered = capsys.readouterr().out

    # Assert
    assert "Identity source" in rendered and "birth_certificate" in rendered


def test_compact_listing_visibly_qualifies_runtime_identity_provenance(capsys) -> None:
    # Arrange
    rows = [_row()]

    # Act
    with _console_width(240):
        print_agent_list(None, rows=rows)
    rendered = capsys.readouterr().out

    # Assert
    assert "Identity source" in rendered and "birth_certificate" in rendered


def test_narrow_details_preserve_full_runtime_identity_and_started_value() -> None:
    # Arrange
    row = {
        **_row(),
        "started_at": "2026-07-12T21:36:30Z",
        "stored_credential": "credential-label",
    }

    # Act
    rendered = "\n".join(_narrow_detail_lines(row, verbose=True))

    # Assert
    assert (
        "Stored credential: credential-label" in rendered,
        "2026-07-13 06:36 (JST)" in rendered,
        "Engine: opencode-go-deepseek-v4.1-flash" in rendered,
        "Identity source: birth_certificate" in rendered,
    ) == (True, True, True, True)
