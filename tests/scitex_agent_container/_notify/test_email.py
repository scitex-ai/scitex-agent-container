"""Tests for the sac-owned email relay config + delegated send.

No mocking: env state is set on the real ``os.environ`` (saved/restored by
the ``email_env`` fixture), config.yaml is written as real bytes under
``tmp_path``, and the SMTP collaborator is injected via ``send_email``'s
``send_fn`` seam with a hand-rolled fake.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._notify.email import (
    DEFAULT_EMAIL_FROM,
    DEFAULT_EMAIL_SMTP_HOST,
    DEFAULT_EMAIL_SMTP_PORT,
    EmailConfig,
    EmailRelayError,
    _ecosystem_email_sender,
    resolve_email_config,
    send_email,
)


def _raise_smtp(**_kwargs):
    """Hand-rolled failing transport (stands in for a dead SMTP server)."""
    raise OSError("smtp down")


@pytest.fixture
def delegated_send(email_env):
    """Run a fully-configured send through a capturing fake transport.

    Returns ``(ok, captured)`` so the return-value and the
    passed-through-args contracts can each be asserted in isolation.
    """
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_FROM"] = "agent@scitex.ai"
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_PASSWORD"] = "pw"
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_TO"] = "op@example.com"
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_SMTP_HOST"] = "smtp.example.com"
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_SMTP_PORT"] = "2525"
    captured: dict = {}
    ok = send_email("subj", "body", send_fn=lambda **kw: captured.update(kw))
    return ok, captured


@pytest.fixture
def disabled_send(email_env):
    """Run a send while the relay is disabled; record any transport calls."""
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_ENABLED"] = "false"
    calls: list = []
    ok = send_email(
        "subj", "body", to="op@example.com", send_fn=lambda **kw: calls.append(kw)
    )
    return ok, calls


class TestResolveDefaults:
    def test_builtin_sender_and_smtp_defaults(self, email_env):
        # Arrange
        # (clean env, no config.yaml — supplied by email_env)
        # Act
        cfg = resolve_email_config(to="x@y.z")
        # Assert
        assert (cfg.from_addr, cfg.smtp_host, cfg.smtp_port, cfg.enabled) == (
            DEFAULT_EMAIL_FROM,
            DEFAULT_EMAIL_SMTP_HOST,
            DEFAULT_EMAIL_SMTP_PORT,
            True,
        )

    def test_recipient_has_no_default(self, email_env):
        # Arrange
        # (no recipient configured anywhere)
        # Act
        cfg = resolve_email_config()
        # Assert
        assert cfg.recipient is None


class TestResolvePrecedence:
    def test_env_overrides_defaults(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_FROM"] = "bot@scitex.ai"
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_SMTP_HOST"] = "smtp.test"
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_SMTP_PORT"] = "2525"
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_TO"] = "op@example.com"
        # Act
        cfg = resolve_email_config()
        # Assert
        assert (cfg.from_addr, cfg.smtp_host, cfg.smtp_port, cfg.recipient) == (
            "bot@scitex.ai",
            "smtp.test",
            2525,
            "op@example.com",
        )

    def test_explicit_to_wins_over_env(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_TO"] = "env@example.com"
        # Act
        cfg = resolve_email_config(to="explicit@example.com")
        # Assert
        assert cfg.recipient == "explicit@example.com"

    def test_config_yaml_email_section_used(self, email_env):
        # Arrange
        email_env.write_text(
            "email:\n"
            "  to: yaml@example.com\n"
            "  from: yamlbot@scitex.ai\n"
            "  smtp_host: smtp.yaml\n"
            "  smtp_port: 1025\n",
            encoding="utf-8",
        )
        # Act
        cfg = resolve_email_config()
        # Assert
        assert (cfg.recipient, cfg.from_addr, cfg.smtp_host, cfg.smtp_port) == (
            "yaml@example.com",
            "yamlbot@scitex.ai",
            "smtp.yaml",
            1025,
        )

    def test_env_overrides_config_yaml(self, email_env):
        # Arrange
        email_env.write_text("email:\n  to: yaml@example.com\n", encoding="utf-8")
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_TO"] = "env@example.com"
        # Act
        cfg = resolve_email_config()
        # Assert
        assert cfg.recipient == "env@example.com"


class TestPasswordIsEnvOnly:
    def test_password_read_from_env(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_PASSWORD"] = "s3cret"
        # Act
        cfg = resolve_email_config(to="x@y.z")
        # Assert
        assert cfg.password == "s3cret"

    def test_password_never_read_from_config_yaml(self, email_env):
        # Arrange
        email_env.write_text(
            "email:\n  password: yaml-secret\n  to: x@y.z\n", encoding="utf-8"
        )
        # Act
        cfg = resolve_email_config()
        # Assert
        assert cfg.password is None


class TestEnabledAndPort:
    def test_enabled_false_via_env(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_ENABLED"] = "false"
        # Act
        cfg = resolve_email_config(to="x@y.z")
        # Assert
        assert cfg.enabled is False

    def test_invalid_enabled_fails_loud(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_ENABLED"] = "maybe"
        ctx = pytest.raises(EmailRelayError, match="boolean")
        # Act
        action = resolve_email_config
        # Assert
        with ctx:
            action(to="x@y.z")

    def test_invalid_port_fails_loud(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_SMTP_PORT"] = "not-a-port"
        ctx = pytest.raises(EmailRelayError, match="smtp_port")
        # Act
        action = resolve_email_config
        # Assert
        with ctx:
            action(to="x@y.z")


class TestSendable:
    def test_returns_pair_when_complete(self):
        # Arrange
        cfg = EmailConfig("a@b.c", "pw", "h", 587, "to@b.c", True)
        # Act
        result = cfg.sendable()
        # Assert
        assert result == ("to@b.c", "pw")

    def test_missing_recipient_fails_loud(self):
        # Arrange
        cfg = EmailConfig("a@b.c", "pw", "h", 587, None, True)
        ctx = pytest.raises(EmailRelayError, match="recipient")
        # Act
        action = cfg.sendable
        # Assert
        with ctx:
            action()

    def test_missing_password_fails_loud(self):
        # Arrange
        cfg = EmailConfig("a@b.c", None, "h", 587, "to@b.c", True)
        ctx = pytest.raises(EmailRelayError, match="password")
        # Act
        action = cfg.sendable
        # Assert
        with ctx:
            action()


class TestSendEmail:
    def test_returns_true_on_send(self, delegated_send):
        # Arrange
        ok, _captured = delegated_send
        # Act
        delivered = ok
        # Assert
        assert delivered is True

    def test_passes_coherent_config_to_transport(self, delegated_send):
        # Arrange
        _ok, captured = delegated_send
        # Act
        passed = captured
        # Assert
        assert passed == {
            "to": "op@example.com",
            "subject": "subj",
            "body": "body",
            "from_addr": "agent@scitex.ai",
            "password": "pw",
            "smtp_host": "smtp.example.com",
            "smtp_port": 2525,
        }

    def test_disabled_returns_false(self, disabled_send):
        # Arrange
        ok, _calls = disabled_send
        # Act
        delivered = ok
        # Assert
        assert delivered is False

    def test_disabled_does_not_call_transport(self, disabled_send):
        # Arrange
        _ok, calls = disabled_send
        # Act
        attempts = calls
        # Assert
        assert attempts == []

    def test_missing_recipient_raises(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_PASSWORD"] = "pw"
        ctx = pytest.raises(EmailRelayError, match="recipient")
        # Act
        action = send_email
        # Assert
        with ctx:
            action("subj", "body")

    def test_missing_password_raises(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_TO"] = "op@example.com"
        ctx = pytest.raises(EmailRelayError, match="password")
        # Act
        action = send_email
        # Assert
        with ctx:
            action("subj", "body")

    def test_transport_error_is_wrapped(self, email_env):
        # Arrange
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_PASSWORD"] = "pw"
        os.environ["SCITEX_AGENT_CONTAINER_EMAIL_TO"] = "op@example.com"
        ctx = pytest.raises(EmailRelayError, match="failed")
        # Act
        action = send_email
        # Assert
        with ctx:
            action("subj", "body", send_fn=_raise_smtp)


class TestDefaultTransport:
    """The un-injected default transport is the real scitex-notification
    sender. scitex-notification is an optional `[notify]` extra, so this is
    skipped (not failed) where it is not installed — the literal
    importorskip also satisfies the PS-210 dev-extras guard."""

    def test_default_transport_is_scitex_notification_send_email(self):
        # Arrange
        pytest.importorskip("scitex_notification")
        import scitex_notification

        # Act
        sender = _ecosystem_email_sender()
        # Assert
        assert sender is scitex_notification.send_email
