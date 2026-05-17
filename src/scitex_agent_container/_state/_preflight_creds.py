"""Pre-dispatch OAuth credential expiry check.

When ``sac agents start`` dispatches an agent that runs Claude via the
Pro/Max OAuth flow, the lead's ``~/.claude/.credentials.json`` is the
authoritative auth artefact — it gets bind-mounted into the container
at ``/tmp/sac-claude/.credentials.json`` by the apptainer runtime
auto-bind (see ``runtimes/_apptainer_runtime.py``).

If the OAuth ``accessToken`` is already expired (or about to expire),
the agent will fail authentication mid-flight without an obvious
cause. This module catches that condition *before* dispatch fires so
the operator gets a clear, actionable message instead of a confusing
runtime error inside the container.

Hard rules (no silent fallbacks):

* Missing credentials file → :class:`FileNotFoundError` with the path
  in the message.
* Unparseable JSON → :class:`ValueError`.
* JSON missing the ``claudeAiOauth`` field → :class:`ValueError`.
* ``claudeAiOauth`` missing the ``expiresAt`` field → :class:`ValueError`.
* Token already expired → :class:`RuntimeError` with seconds-ago count.
* Token expiring within 5 minutes → :class:`RuntimeError` with seconds-
  remaining count.

The check is skipped entirely when the operator opts out of OAuth by
setting ``ANTHROPIC_API_KEY`` or ``SAC_ANTHROPIC_API_KEY`` in env (the
API-key auth path; see ``runtimes/_sdk_common.provision_anthropic_auth``).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Skew threshold: refuse to dispatch when the token has less than this
# many seconds of life left. Five minutes is the practical lower bound —
# claude-code's own refresh loop wakes roughly at the 5-min mark, and a
# fresh agent starting under a sub-300s token is virtually guaranteed
# to fail mid-session.
EXPIRY_SKEW_SECONDS = 300


def _default_credentials_path() -> Path:
    """Return the canonical lead-host credentials path.

    Kept as a helper (not a module-level constant) so tests can monkey-
    free-replace it via the ``creds_path`` parameter without touching
    ``~/.claude/`` on disk.
    """
    return Path.home() / ".claude" / ".credentials.json"


def _api_key_env_is_set() -> bool:
    """True when the operator has opted into the API-key auth path.

    ``ANTHROPIC_API_KEY`` is the SDK-native env var; ``SAC_ANTHROPIC_API_KEY``
    is sac's preferred form that gets mirrored into ``ANTHROPIC_API_KEY``
    by ``runtimes/_sdk_common.provision_anthropic_auth`` at runtime. Either
    indicates the operator is not relying on the OAuth ``credentials.json``
    so the expiry check is moot.
    """
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SAC_ANTHROPIC_API_KEY")
    )


def check_oauth_token_expiry(
    creds_path: Path | None = None,
    *,
    now: float | None = None,
    skew_seconds: int = EXPIRY_SKEW_SECONDS,
) -> None:
    """Raise if the lead-side OAuth token is missing, malformed, or expired.

    Parameters
    ----------
    creds_path
        Path to the credentials JSON. Defaults to
        ``~/.claude/.credentials.json`` on the lead host. Tests pass an
        explicit ``tmp_path`` to avoid touching the operator's real file.
    now
        Override for the wall clock (unix seconds). Defaults to
        ``time.time()``. Tests use this to pin the comparison instant.
    skew_seconds
        Refuse to dispatch when the token has fewer than this many
        seconds of life remaining. Defaults to
        :data:`EXPIRY_SKEW_SECONDS` (300 = 5 minutes).

    Raises
    ------
    FileNotFoundError
        When the credentials file does not exist. The message includes
        the absolute path so the operator knows where ``claude login``
        is expected to write.
    ValueError
        When the file is not valid JSON, or when it is valid JSON but
        does not contain the expected ``claudeAiOauth.expiresAt`` shape.
    RuntimeError
        When the token has already expired, or when its remaining
        lifetime is below ``skew_seconds``. The message tells the
        operator how to refresh it (``claude login``).
    """
    if _api_key_env_is_set():
        return

    path = creds_path if creds_path is not None else _default_credentials_path()
    now_ts = now if now is not None else time.time()

    if not path.is_file():
        raise FileNotFoundError(
            f"OAuth credentials file not found at {path!s}. "
            "Run `claude login` to create it, or export "
            "ANTHROPIC_API_KEY / SAC_ANTHROPIC_API_KEY to use the "
            "API-key auth path instead."
        )

    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"OAuth credentials file at {path!s} is not valid JSON: {exc}. "
            "Run `claude login` to regenerate it."
        ) from exc

    if not isinstance(data, dict) or "claudeAiOauth" not in data:
        raise ValueError(
            f"OAuth credentials file at {path!s} is missing the "
            "`claudeAiOauth` field. Run `claude login` to regenerate it."
        )

    oauth = data["claudeAiOauth"]
    if not isinstance(oauth, dict) or "expiresAt" not in oauth:
        raise ValueError(
            f"OAuth credentials file at {path!s} is missing the "
            "`claudeAiOauth.expiresAt` field. Run `claude login` to "
            "regenerate it."
        )

    expires_at_raw = oauth["expiresAt"]
    # claude-code writes expiresAt as a unix-MILLISECOND timestamp (not
    # seconds), so divide by 1000 before comparing. We accept either
    # representation by detecting the magnitude — any value > 1e12 is
    # clearly milliseconds (would otherwise place us in year ~33000+).
    try:
        expires_at_num = float(expires_at_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"OAuth credentials file at {path!s} has non-numeric "
            f"`claudeAiOauth.expiresAt={expires_at_raw!r}`. Run "
            "`claude login` to regenerate it."
        ) from exc

    expires_at_seconds = (
        expires_at_num / 1000.0 if expires_at_num > 1e12 else expires_at_num
    )

    delta = expires_at_seconds - now_ts

    if delta <= 0:
        seconds_ago = int(-delta)
        raise RuntimeError(
            f"OAuth token in {path!s} expired {seconds_ago} seconds ago. "
            "Run `claude login` to refresh."
        )

    if delta < skew_seconds:
        seconds_left = int(delta)
        raise RuntimeError(
            f"OAuth token in {path!s} expires in {seconds_left} seconds "
            f"(< {skew_seconds // 60} min). Refresh with `claude login` "
            "before launching long-lived agents."
        )


__all__ = [
    "EXPIRY_SKEW_SECONDS",
    "check_oauth_token_expiry",
]
