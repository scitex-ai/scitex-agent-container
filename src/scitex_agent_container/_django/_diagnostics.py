"""Sanitized diagnostics for the Agents dashboard.

A failure needs to be correlatable by support WITHOUT the correlation itself
becoming an information leak. The rule here is that the diagnostic ID is
DERIVED, never quoted: a raw exception string is exactly the kind of value that
carries a Bearer token, a credentialed URL, a spec path, or an operator's
home directory, and this app already crosses that boundary once (``_remote``
drops spec paths and state dirs on purpose — see ``_projection``).

So the ID is a truncated digest of a canonical cause CATEGORY, not of the
free-text message: two operators hitting the same failure on different hosts
get the SAME id (the point of a diagnostic id), while nothing about either
host's contents can be recovered from it.
"""

from __future__ import annotations

import hashlib

# Cause categories. Deliberately coarse — the category is what a responder acts
# on, and a finer key would start to fingerprint the deployment.
_CAUSES = {
    "auth_rejected": "the listener refused our credential",
    "listener_unreachable": "no answer from the configured listener",
    "listener_timeout": "the configured listener did not answer in time",
    "listener_error": "the listener answered with an error",
    "listener_malformed": "the listener answered in an unexpected shape",
    "unknown": "the failure did not match a known category",
}


def classify(error: BaseException | str | None) -> str:
    """Map a failure to one of the fixed cause categories, or ``unknown``."""
    text = str(error or "").lower()
    if not text:
        return "unknown"
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
        return "auth_rejected"
    if "no response within" in text:
        return "listener_timeout"
    if "timed out" in text or "timeout" in text:
        return "listener_timeout"
    if "no agents list" in text or "non-object json" in text:
        return "listener_malformed"
    if "could not reach" in text or "connection refused" in text or "name or service" in text:
        return "listener_unreachable"
    return "listener_error"


def diagnostic_id(error: BaseException | str | None) -> str:
    """A stable, sanitized ``diag-<12 hex>`` for a failure — never its text."""
    digest = hashlib.sha256(classify(error).encode("utf-8")).hexdigest()[:12]
    return f"diag-{digest}"


def diagnostic_summary(error: BaseException | str | None) -> str:
    """The operator-facing sentence for a cause category (no raw error text)."""
    return _CAUSES[classify(error)]


__all__ = ["classify", "diagnostic_id", "diagnostic_summary"]
