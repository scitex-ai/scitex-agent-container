"""Classify SDK conversation failures that are really an auth/credential death.

When a long-lived ``claude-session`` runner loses its Anthropic
authentication mid-flight — the Pro/Max OAuth token in
``~/.claude/.credentials.json`` expired or rotated out from under the
running session — the ``claude-agent-sdk`` does not surface a typed
exception. It bubbles up as a generic ``ProcessError`` /
``RuntimeError`` whose message contains an HTTP ``401`` or an
``invalid api key`` / ``OAuth token has expired`` phrase.

Without classification that failure lands in the conversation
supervisor's catch-all and is recorded with the ambiguous
``cause="sdk-crash"`` — indistinguishable from a network blip or an
SDK panic. The operator then "finds out only by noticing silence."

This module turns that silent, ambiguous death into a LOUD, specific
signal: :func:`classify_auth_failure` recognises the auth signatures
and returns a clear operator-facing message that names the cause and
carries the manual-refresh hint (``claude login``). The supervisor
uses it to (a) flip the recorded ``cause`` to ``auth-expired`` and
(b) emit the hint-bearing detail, so ``sac agent status`` / the error
diary show *exactly* what went wrong and how to fix it.

Scope is detection + a clear message only. Refreshing the token is the
operator's manual recovery path (run ``claude login``); no auto-rotation
is performed here (deferred to FUTURE).

Pure stdlib, no SDK import — so it is unit-testable against plain
strings / exceptions without the optional ``claude-agent-sdk`` present.
"""

from __future__ import annotations

__all__ = ["AUTH_FAILURE_CAUSE", "REFRESH_HINT", "classify_auth_failure"]

# Short ``cause`` identifier written to ``state.db.errors``. Distinct
# from the generic ``sdk-crash`` so the lead can group on it and the
# operator can see at a glance that this is a credential problem, not a
# transient network/SDK fault.
AUTH_FAILURE_CAUSE = "auth-expired"

# The single manual-recovery instruction. Kept as one constant so the
# wording stays identical everywhere an auth failure is reported.
REFRESH_HINT = (
    "Anthropic auth failed — the OAuth token is expired/invalid. "
    "Run `claude login` on the agent's host to refresh "
    "~/.claude/.credentials.json, then restart the agent."
)

# Lower-cased substrings that mark a failure as an auth/credential death
# rather than a generic SDK/network fault. Matched against the
# stringified exception. Kept deliberately narrow so a tool-result blob
# that merely *mentions* "401" in unrelated content doesn't get
# misclassified — every needle here is an Anthropic auth-rejection
# phrase, not a generic token.
_AUTH_SIGNATURES = (
    "401",
    "unauthorized",
    "invalid api key",
    "invalid x-api-key",
    "authentication_error",
    "authentication error",
    "oauth token has expired",
    "oauth token expired",
    "token has expired",
    "could not refresh",
    "invalid bearer token",
    "credit balance is too low",
    # Phrases emitted by provision_anthropic_auth / check_oauth_token_expiry
    # when the on-disk credentials file is missing, malformed, or holds an
    # expired token. Surfacing these as auth-expired (not sdk-options)
    # gives the operator the same loud, hint-bearing record.
    "oauth credentials",
    "oauth token in",
    "no anthropic auth available",
    "run `claude login`",
    "run `claude /login`",
)


def classify_auth_failure(error: object) -> str | None:
    """Return a loud, hint-bearing message if ``error`` is an auth death.

    Parameters
    ----------
    error
        The exception (or any object) raised by the SDK conversation.
        Stringified and scanned case-insensitively for the Anthropic
        auth-rejection signatures in :data:`_AUTH_SIGNATURES`.

    Returns
    -------
    str
        A clear operator-facing message — the original error text
        followed by :data:`REFRESH_HINT` — when the failure looks like
        an expired/invalid credential. The caller records this with
        ``cause`` :data:`AUTH_FAILURE_CAUSE`.
    None
        When the failure does not match any auth signature; the caller
        keeps its existing generic handling (``cause="sdk-crash"``).
    """
    text = str(error)
    haystack = text.lower()
    if not any(sig in haystack for sig in _AUTH_SIGNATURES):
        return None
    detail = text.strip()
    if detail:
        return f"{detail} | {REFRESH_HINT}"
    return REFRESH_HINT
