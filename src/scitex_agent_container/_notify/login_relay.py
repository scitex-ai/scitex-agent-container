"""OAuth login-relay helpers — scrape the auth URL, email it to the operator.

The orchestration lives in the auto-accept responder
(``_runners/_tmux/auto/accept.py``): when an agent's TUI shows the Claude
login wall, the responder sends ``/login``; on the next tick the OAuth
authorize URL appears, and the responder calls :func:`send_login_url_email`
so the operator can complete the browser sign-in and relay the code back
via ``sac send <agent> <code>`` — which the A2A turn-bridge types straight
into the waiting login prompt.

This module only knows how to *find* the URL in pane text and *format +
send* the operator email; the detection-state plumbing lives in
``_state._meta.pane._classify_pane_state`` and the action dispatch in the
responder.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .email import send_email

log = logging.getLogger(__name__)

# A Claude OAuth authorize URL (claude.ai or console.anthropic.com). tmux
# captures the pane with ``-J`` (join wrapped lines), so the long URL arrives
# on a single logical line and matches in one piece. Stop at whitespace or a
# quote/bracket so a trailing render artifact isn't swallowed into the URL.
_OAUTH_URL_RE = re.compile(r"https?://[^\s'\"<>`]*oauth[^\s'\"<>`]*", re.IGNORECASE)


def extract_oauth_url(pane_text: str) -> str | None:
    """Return the first OAuth authorize URL visible in ``pane_text``, else None."""
    if not pane_text:
        return None
    match = _OAUTH_URL_RE.search(pane_text)
    return match.group(0) if match else None


def send_login_url_email(
    agent_name: str,
    url: str,
    *,
    to: str | None = None,
    send_fn: Callable[..., Any] | None = None,
) -> bool:
    """Email the operator the OAuth URL plus how to relay the code back.

    Returns whatever :func:`._notify.email.send_email` returns (``True`` on
    send, ``False`` if the relay is disabled); raises ``EmailRelayError`` on
    a real failure (fail-loud). ``send_fn`` is threaded straight through as
    the injectable transport seam.
    """
    subject = f"[SAC] {agent_name}: Claude login required"
    body = (
        f"Agent '{agent_name}' hit the Claude login wall, so sac ran /login.\n\n"
        f"1. Open this URL in a browser and sign in:\n\n"
        f"   {url}\n\n"
        f"2. Copy the authorization code Claude shows after you approve.\n\n"
        f"3. Relay the code back to the agent (it is typed into the waiting\n"
        f"   login prompt — no shell access to the agent's host needed beyond\n"
        f"   wherever you run sac):\n\n"
        f"   sac send {agent_name} <code>\n\n"
        f"The agent resumes automatically once the code is accepted.\n"
    )
    log.info("emailing OAuth login URL for agent %s", agent_name)
    return send_email(subject, body, to=to, send_fn=send_fn)
