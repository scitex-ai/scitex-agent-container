"""Layer 3 — action: respond(name, state) -> bool.

Maps a classified pane state to a concrete action:
  - compose_pending_unsent  → send Enter
  - y_n_prompt              → verify [1] Yes present, then send 1 + Enter
  - auth_error              → DM mgr-auth (escalate, no keys sent)
  - limit_reached           → DM healer  (escalate, no keys sent)
  - anything else           → no-op

Returns True if a key action was sent, False for no-op / escalate.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Callable

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
            ["tmux", "-L", _TMUX_SERVER, "send-keys", "-t", session, key, ""],
            check=False,
        )
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Orochi DM escalation
# ---------------------------------------------------------------------------


def _orochi_dm(channel: str, message: str) -> None:
    """Send a DM to an Orochi channel via the hub HTTP API.

    Uses environment variables:
      SCITEX_OROCHI_HUB_URL   (default: https://scitex-orochi.com)
      SCITEX_OROCHI_TOKEN     (required for auth)

    Falls back to logging when the hub is unreachable or credentials
    are absent so that the daemon never crashes on escalation failure.
    """
    hub = os.environ.get("SCITEX_OROCHI_HUB_URL", "https://scitex-orochi.com").rstrip(
        "/"
    )
    token = os.environ.get("SCITEX_OROCHI_TOKEN", "")
    agent = os.environ.get("SCITEX_OROCHI_AGENT", "c-sac-auto-accept")

    if not token:
        logger.warning("SCITEX_OROCHI_TOKEN not set — DM to %s dropped: %s", channel, message)
        return

    payload = f'{{"channel":"{channel}","text":"{message}","from":"{agent}"}}'
    try:
        subprocess.run(
            [
                "curl",
                "-s",
                "-X", "POST",
                "-H", f"Authorization: Bearer {token}",
                "-H", "Content-Type: application/json",
                "-d", payload,
                f"{hub}/api/v1/messages",
            ],
            check=False,
            timeout=10,
            capture_output=True,
        )
        logger.info("DM sent to %s: %s", channel, message)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        logger.warning("DM to %s failed: %s — message: %s", channel, exc, message)


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

    Returns
    -------
    True if keys were sent; False for no-op or escalate-only actions.
    """
    _send = send_fn if send_fn is not None else lambda *keys: _tmux_send(name, *keys)
    _dm = dm_fn if dm_fn is not None else _orochi_dm

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
        _dm("mgr-auth", f"[{name}] auth_error detected — please rotate credentials")
        logger.warning("[%s] auth_error → escalated to mgr-auth", name)
        return False

    if state == "limit_reached":
        _dm("healer", f"[{name}] limit_reached — quota exhausted")
        logger.warning("[%s] limit_reached → escalated to healer", name)
        return False

    # Any other state: no-op
    return False
