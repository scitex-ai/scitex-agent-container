"""Layer 3 — action: respond(name, state) -> bool.

Maps a classified pane state to a concrete action:
  - compose_pending_unsent  → send Enter
  - y_n_prompt              → verify [1] Yes present, then send 1 + Enter
  - auth_error              → send /login (initiate re-auth) + DM mgr-auth
  - login_url               → email the OAuth URL to the operator (relay)
  - limit_reached           → DM healer  (escalate, no keys sent)
  - anything else           → no-op

Returns True if a key action was sent, False for no-op / escalate.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Callable

from scitex_agent_container._notify.login_relay import (
    extract_oauth_url,
    send_login_url_email,
)
from scitex_agent_container._runners._tmux._target import exact_target

logger = logging.getLogger(__name__)

_TMUX_SERVER = "sac"


# ---------------------------------------------------------------------------
# Low-level tmux send
# ---------------------------------------------------------------------------


def _tmux_send(name: str, *keys: str) -> None:
    """Send *keys* to the sac-<name> tmux pane, one at a time."""
    session = f"sac-{name}"
    for key in keys:
        subprocess.run(
            [
                "tmux",
                "-L",
                _TMUX_SERVER,
                "send-keys",
                "-t",
                exact_target(session),
                key,
                "",
            ],
            check=False,
        )
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Escalation (log-only default)
# ---------------------------------------------------------------------------


def _log_escalation(channel: str, message: str) -> None:
    """Default escalation sink — record locally, no outbound push.

    sac's comms boundary is one-way: an external hub reads sac's state;
    sac never pushes to it. The auto-accept daemon therefore has no
    transport of its own — it logs the escalation and a consumer that
    tails these records acts on it. Callers inject ``dm_fn`` to attach a
    real transport.
    """
    logger.warning("escalation [%s]: %s", channel, message)


# ---------------------------------------------------------------------------
# y/n prompt verification
# ---------------------------------------------------------------------------


def _yn_has_yes_option(pane_text: str) -> bool:
    """Return True iff the pane contains a [1] Yes or 1. Yes option.

    Critical safety check: never blind-press 1 without confirming
    the dialog actually offers 1=Yes.
    """
    lower = pane_text[-2000:].lower()
    return "[1] yes" in lower or "1. yes" in lower or "1) yes" in lower


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def respond(
    name: str,
    state: str,
    pane_text: str = "",
    *,
    send_fn: Callable[..., None] | None = None,
    dm_fn: Callable[[str, str], None] | None = None,
    email_fn: Callable[[str], bool] | None = None,
) -> bool:
    """Dispatch the action for *state* on agent *name*.

    Parameters
    ----------
    name:
        Agent name (used to derive tmux session ``sac-<name>``).
    state:
        Classified pane state from ``_classify_pane_state``.
    pane_text:
        Raw pane content (required for y_n_prompt verification).
    send_fn:
        Override for tmux send (injected in tests).
    dm_fn:
        Override for DM escalation (injected in tests).
    email_fn:
        Override for the login-URL email send (injected in tests); takes
        the OAuth URL and returns True on send. Defaults to
        ``send_login_url_email(name, url)``.

    Returns
    -------
    True if keys were sent; False for no-op or escalate-only actions.
    """
    _send = send_fn if send_fn is not None else lambda *keys: _tmux_send(name, *keys)
    _dm = dm_fn if dm_fn is not None else _log_escalation
    _email = (
        email_fn
        if email_fn is not None
        else (lambda url: send_login_url_email(name, url))
    )

    if state == "compose_pending_unsent":
        _send("Enter")
        logger.info("[%s] compose_pending_unsent → sent Enter", name)
        return True

    if state == "y_n_prompt":
        if not _yn_has_yes_option(pane_text):
            logger.warning(
                "[%s] y_n_prompt: [1] Yes not found in pane — skipping blind send",
                name,
            )
            return False
        _send("1")
        _send("Enter")
        logger.info("[%s] y_n_prompt → sent 1 + Enter (verified [1] Yes present)", name)
        return True

    if state == "auth_error":
        # The login wall. Initiate re-auth by sending `/login`; the OAuth
        # authorize URL appears on the next pane (state "login_url") and is
        # emailed to the operator from there.
        _send("/login")
        _send("Enter")
        _dm("mgr-auth", f"[{name}] auth_error — sent /login; OAuth URL will be emailed")
        logger.warning("[%s] auth_error → sent /login (re-auth initiated)", name)
        return True

    if state == "login_url":
        url = extract_oauth_url(pane_text)
        if not url:
            logger.warning("[%s] login_url state but no OAuth URL in pane", name)
            return False
        try:
            _email(url)
        except Exception as exc:  # stx-allow: fallback (reason: an email failure must not crash the auto-accept daemon loop — escalated via DM instead)
            logger.error("[%s] login_url → email failed: %s", name, exc)
            _dm("mgr-auth", f"[{name}] OAuth URL ready but email failed: {exc}")
            return False
        logger.info("[%s] login_url → emailed OAuth URL to operator", name)
        return False

    if state == "limit_reached":
        _dm("healer", f"[{name}] limit_reached — quota exhausted")
        logger.warning("[%s] limit_reached → escalated to healer", name)
        return False

    # Any other state: no-op
    return False
