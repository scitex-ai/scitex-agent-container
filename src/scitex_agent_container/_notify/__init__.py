"""Operator-facing notification channels owned by sac.

Currently exposes the email relay used by the OAuth login-relay workflow
(``_runners/_tmux/auto``). sac owns the *configuration* (recipient,
sender, SMTP coordinates, enable flag) under its own
``SAC_* / SCITEX_AGENT_CONTAINER_*`` namespace; the actual SMTP send is
delegated to scitex-notification's public ``send_email`` so the ecosystem
keeps a single notification surface.
"""

from __future__ import annotations

from .email import (
    DEFAULT_EMAIL_FROM,
    DEFAULT_EMAIL_SMTP_HOST,
    DEFAULT_EMAIL_SMTP_PORT,
    EmailConfig,
    EmailRelayError,
    resolve_email_config,
    send_email,
)
from .login_relay import extract_oauth_url, send_login_url_email

__all__ = [
    "DEFAULT_EMAIL_FROM",
    "DEFAULT_EMAIL_SMTP_HOST",
    "DEFAULT_EMAIL_SMTP_PORT",
    "EmailConfig",
    "EmailRelayError",
    "extract_oauth_url",
    "resolve_email_config",
    "send_email",
    "send_login_url_email",
]
