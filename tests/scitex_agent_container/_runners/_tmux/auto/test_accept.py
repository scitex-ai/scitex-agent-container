"""Smoke surface for ``_runners/_tmux/auto/accept.py``.

The substantive auto-accept logic is dormant behind the TUI hedge
flag (``spec.runtime: tui``) and is exercised end-to-end by the
follow-up integration PR. Until then these tests pin the import
surface and the public callable shape so a regression that drops
``respond`` from the module surface fails CI loudly. One assert
per test, AAA markers each on their own line per STX-TQ002/007.
"""

from __future__ import annotations

import logging

from scitex_agent_container._runners._tmux.auto import accept as A


def test_respond_callable_exists_on_module_surface() -> None:
    # Arrange
    module = A
    # Act
    obj = getattr(module, "respond", None)
    # Assert
    assert callable(obj)


def test_yn_has_yes_option_returns_true_for_classic_one_yes_prompt() -> None:
    # Arrange
    pane = "Continue? [1] Yes [2] No"
    # Act
    matched = A._yn_has_yes_option(pane)
    # Assert
    assert matched is True


def test_yn_has_yes_option_returns_false_for_plain_text() -> None:
    # Arrange
    pane = "No prompt here, just narrative output."
    # Act
    matched = A._yn_has_yes_option(pane)
    # Assert
    assert matched is False


def test_respond_auth_error_sends_slash_login() -> None:
    # Arrange
    keys: list = []
    # Act
    A.respond(
        "cap-002",
        "auth_error",
        pane_text="please run /login",
        send_fn=lambda *k: keys.append(k[0]),
        dm_fn=lambda _channel, _msg: None,
    )
    # Assert
    assert keys == ["/login", "Enter"]


def test_respond_login_url_emails_the_extracted_url() -> None:
    # Arrange
    captured: dict = {}

    def _capture_email(url):
        captured["url"] = url
        return True

    pane = "sign in:\nhttps://claude.ai/oauth/authorize?code=true\npaste code"
    # Act
    A.respond(
        "cap-002",
        "login_url",
        pane_text=pane,
        send_fn=lambda *k: None,
        dm_fn=lambda _channel, _msg: None,
        email_fn=_capture_email,
    )
    # Assert
    assert captured["url"] == "https://claude.ai/oauth/authorize?code=true"


def test_respond_login_url_without_url_skips_email() -> None:
    # Arrange
    calls: list = []

    def _record_email(url):
        calls.append(url)
        return True

    # Act
    A.respond(
        "cap-002",
        "login_url",
        pane_text="no authorize link in this pane",
        send_fn=lambda *k: None,
        dm_fn=lambda _channel, _msg: None,
        email_fn=_record_email,
    )
    # Assert
    assert calls == []


def test_default_escalation_logs_locally_without_outbound_push(caplog) -> None:
    # Arrange
    caplog.set_level(logging.WARNING)
    # Act
    A.respond("cap-002", "limit_reached", send_fn=lambda *k: None)
    # Assert
    assert "escalation [healer]" in caplog.text
