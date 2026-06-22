"""Email relay channel — sac-owned configuration + delegated send.

sac owns the email *configuration*; scitex-notification owns the SMTP
*send*. This split keeps email config in sac's namespace (so the
login-relay can be driven the same way as every other sac knob) while
reusing the ecosystem notifier rather than reimplementing smtplib.

Resolution precedence (highest first)::

    1. explicit kwargs to resolve_email_config / send_email
    2. env vars, read via _env.getenv (so SAC_EMAIL_* AND
       SCITEX_AGENT_CONTAINER_EMAIL_* are both honoured, and a
       disagreement between the two forms fails loud):
         EMAIL_FROM  EMAIL_PASSWORD  EMAIL_SMTP_HOST
         EMAIL_SMTP_PORT  EMAIL_TO  EMAIL_ENABLED
    3. <proj-root>/.scitex/agent-container/config.yaml  `email:` section
       (project scope wins over user scope — same cascade as host_config)
    4. ~/.scitex/agent-container/config.yaml            `email:` section
    5. built-in defaults

Built-in defaults: sender ``agent@scitex.ai``, SMTP
``mail1030.onamae.ne.jp:587``, ``enabled=True``. The recipient has NO
default — the operator MUST set it (``SCITEX_AGENT_CONTAINER_EMAIL_TO``
or ``email.to`` in config.yaml); a send with no recipient fails loud.

SECURITY: the password is read from the environment ONLY — never from
config.yaml (which is routinely committed to a project repo). Put the
secret in the shell secrets layer (``01_agent-container.src``), not in
a yaml file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .._env import getenv

log = logging.getLogger(__name__)

# Built-in defaults. The sender is the shared sac agent identity; the SMTP
# host is its provider (onamae, not gmail — so the host default matters: an
# agent@scitex.ai login against smtp.gmail.com would be rejected).
DEFAULT_EMAIL_FROM = "agent@scitex.ai"
DEFAULT_EMAIL_SMTP_HOST = "mail1030.onamae.ne.jp"
DEFAULT_EMAIL_SMTP_PORT = 587


class EmailRelayError(RuntimeError):
    """Raised when an email cannot be configured or sent.

    Always carries an operator-actionable message (which env var or
    config key to set, or which package to install) — the relay must
    never fail silently.
    """


@dataclass(frozen=True)
class EmailConfig:
    """Resolved, coherent email identity + destination.

    ``from_addr``/``password``/``smtp_host``/``smtp_port`` are resolved
    together so the SMTP login uses a matching credential pair (the bug
    scitex-notification's independent env-fallback chains can hit).
    """

    from_addr: str
    password: str | None
    smtp_host: str
    smtp_port: int
    recipient: str | None
    enabled: bool

    def sendable(self) -> tuple[str, str]:
        """Return ``(recipient, password)`` or fail loud if either is unset."""
        if not self.recipient:
            raise EmailRelayError(
                "no email recipient configured — set "
                "SCITEX_AGENT_CONTAINER_EMAIL_TO (or SAC_EMAIL_TO), or "
                "`email.to` in ~/.scitex/agent-container/config.yaml."
            )
        if not self.password:
            raise EmailRelayError(
                "no email password configured — set "
                "SCITEX_AGENT_CONTAINER_EMAIL_PASSWORD (or SAC_EMAIL_PASSWORD) "
                "in your shell secrets layer (never in config.yaml)."
            )
        return self.recipient, self.password


def _config_yaml_email_section() -> dict[str, Any]:
    """Return the ``email:`` mapping from sac's config.yaml, or ``{}``.

    Uses the same path resolution as ``_state.host_config`` —
    ``$SCITEX_AGENT_CONTAINER_CONFIG`` override, else the SciTeX
    local-state cascade (project scope preferred over user scope).
    Missing file / missing section → ``{}`` (tolerated). A *corrupt*
    yaml file raises (loud) rather than silently yielding ``{}``.
    """
    override = os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG")
    if override:
        path: Path | None = Path(override)
    else:
        # stx-allow: fallback (reason: scitex_config is a hard dep, but a
        # broken install must not wedge the relay's config read — env vars
        # alone can still configure it. Surfaced via a warning, not silence.)
        try:
            from scitex_config._ecosystem import local_state

            path = local_state.path("agent-container", "config.yaml")
        except Exception as exc:  # pragma: no cover
            log.warning("could not resolve config.yaml path: %s", exc)
            path = None

    if path is None or not path.exists():
        return {}

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise EmailRelayError(
            f"{path} is not a YAML mapping; cannot read `email:` section."
        )
    section = data.get("email") or {}
    if not isinstance(section, dict):
        raise EmailRelayError(
            f"`email:` in {path} must be a mapping, got {type(section).__name__}."
        )
    return section


def _as_bool(value: Any, default: bool) -> bool:
    """Parse a yaml/env truthy value; unknown strings fail loud."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise EmailRelayError(f"cannot parse boolean from {value!r} (use true/false).")


def resolve_email_config(*, to: str | None = None) -> EmailConfig:
    """Resolve the effective email config from kwargs > env > yaml > defaults."""
    cfg = _config_yaml_email_section()

    from_addr = getenv("EMAIL_FROM") or cfg.get("from") or DEFAULT_EMAIL_FROM
    # Password: environment ONLY (never config.yaml — see module docstring).
    password = getenv("EMAIL_PASSWORD")
    smtp_host = (
        getenv("EMAIL_SMTP_HOST") or cfg.get("smtp_host") or DEFAULT_EMAIL_SMTP_HOST
    )

    port_raw = (
        getenv("EMAIL_SMTP_PORT") or cfg.get("smtp_port") or DEFAULT_EMAIL_SMTP_PORT
    )
    try:
        smtp_port = int(port_raw)
    except (TypeError, ValueError) as exc:
        raise EmailRelayError(
            f"email smtp_port must be an integer, got {port_raw!r}."
        ) from exc

    recipient = to or getenv("EMAIL_TO") or cfg.get("to")
    enabled = _as_bool(getenv("EMAIL_ENABLED"), _as_bool(cfg.get("enabled"), True))

    return EmailConfig(
        from_addr=str(from_addr),
        password=password,
        smtp_host=str(smtp_host),
        smtp_port=smtp_port,
        recipient=str(recipient) if recipient else None,
        enabled=enabled,
    )


def _ecosystem_email_sender() -> Callable[..., Any]:
    """Return scitex-notification's ``send_email``, or fail loud if absent.

    Isolated so the lazy import is a single, testable seam; the failure
    mode is an actionable :class:`EmailRelayError`, never a bare
    ``ImportError`` leaking out of the relay.
    """
    try:
        from scitex_notification import send_email as _send
    except ImportError as exc:
        raise EmailRelayError(
            "scitex-notification is required to send email; install the extra "
            "with `pip install scitex-agent-container[notify]`."
        ) from exc
    return _send


def send_email(
    subject: str,
    body: str,
    *,
    to: str | None = None,
    send_fn: Callable[..., Any] | None = None,
) -> bool:
    """Send a plain-text email via the sac-resolved config.

    Parameters
    ----------
    send_fn:
        Delivery callable invoked with keyword args ``to``, ``subject``,
        ``body``, ``from_addr``, ``password``, ``smtp_host``,
        ``smtp_port``. Defaults to :func:`scitex_notification.send_email`.
        Injectable so callers (and tests) can substitute the transport
        without monkeypatching — mirrors the ``*_fn`` seams used by the
        auto-accept daemon.

    Returns ``True`` on send. Returns ``False`` ONLY when email is
    explicitly disabled (``EMAIL_ENABLED=false`` / ``email.enabled:
    false``) — a deliberate operator opt-out, logged so it is visible.
    Every other failure (no recipient, no password, scitex-notification
    missing, SMTP error) raises :class:`EmailRelayError` — fail loud.
    """
    cfg = resolve_email_config(to=to)

    if not cfg.enabled:
        log.warning(
            "email relay disabled (EMAIL_ENABLED=false) — not sending %r", subject
        )
        return False

    recipient, password = cfg.sendable()
    sender = send_fn if send_fn is not None else _ecosystem_email_sender()

    try:
        sender(
            to=recipient,
            subject=subject,
            body=body,
            from_addr=cfg.from_addr,
            password=password,
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
        )
    except Exception as exc:
        raise EmailRelayError(
            f"email send to {recipient!r} via {cfg.smtp_host}:{cfg.smtp_port} "
            f"failed: {exc}"
        ) from exc

    log.info("email sent to %s (subject: %s)", recipient, subject)
    return True
