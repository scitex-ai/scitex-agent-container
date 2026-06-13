"""Classify an Anthropic auth token by prefix; refuse OAuth-as-api-key.

Fleet failure mode (lead, top-5):
    *OAuth-token-as-api-key mismap.* When the agent's Anthropic auth
    carries an OAuth token (``sk-ant-oat-...``) but the runner / SDK
    mis-classifies it as an API key (``ANTHROPIC_API_KEY=<oat>``),
    every request 401s silently and the agent goes "alive but
    silent". The supervisor sees a running process; the operator
    sees nothing — exactly the "alive but silent" stall.

Background — the two Anthropic credential shapes:

* **OAuth access token** — issued by ``claude login`` (Pro/Max). Prefix
  ``sk-ant-oat-``. NOT a valid ``ANTHROPIC_API_KEY`` value; the API
  rejects it as ``invalid api key``. The canonical wiring is the
  ``.credentials.json`` file bound into the container at
  ``CLAUDE_CONFIG_DIR=/tmp/sac-claude`` — the in-container SDK reads
  the file directly and the OAuth flow rotates ``accessToken`` in
  place. See ``runtimes/_apptainer_auth.py``.

* **API key** — issued from the Anthropic console (pay-per-token).
  Prefix ``sk-ant-api-``. This is what ``ANTHROPIC_API_KEY`` /
  ``SAC_ANTHROPIC_API_KEY`` are for; the SDK uses the bare env var.

This module is the prefix classifier + the defensive guard:

* :func:`is_oauth_token` — True for ``sk-ant-oat-…``.
* :func:`is_api_key`   — True for ``sk-ant-api-…``.
* :func:`classify`     — returns ``"oauth"`` / ``"api_key"`` /
  ``"unknown"`` (the explicit three-way so a caller can treat
  "unknown" however its risk profile demands).
* :func:`assert_api_key` — LOUDLY refuses an OAuth token in an
  API-key slot. The error names the prefix it saw, names the
  canonical remediation, and points at the credentials-bind path.

Phase 1 lands the classifier + tests only. The caller wiring
(injecting this guard into ``_apptainer_auth.auth_argv`` /
``_state._preflight_creds`` so a misrouted OAuth token never reaches
``ANTHROPIC_API_KEY=...``) is a follow-up PR.

Pure stdlib, no SDK import — unit-testable as a pure prefix check.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "OAUTH_PREFIX",
    "API_KEY_PREFIX",
    "is_oauth_token",
    "is_api_key",
    "classify",
    "assert_api_key",
]

# Canonical Anthropic credential prefixes (2026-06).
#
# These are stable enough to defend on — the OAuth/api-key split has
# been the Anthropic shape since ``claude login`` shipped, and the
# operator-facing remediation hinges on the user reading the prefix
# correctly. If Anthropic adds a third shape, ``classify`` returns
# ``"unknown"`` and ``assert_api_key`` accepts it (fail-open on
# unrecognised; fail-closed only on the *known* OAuth shape, which
# is the documented mismap).
OAUTH_PREFIX = "sk-ant-oat-"
API_KEY_PREFIX = "sk-ant-api-"


def is_oauth_token(token: str) -> bool:
    """Return True iff ``token`` is an Anthropic OAuth access token.

    Recognised by the canonical ``sk-ant-oat-`` prefix. Whitespace is
    stripped because operators routinely paste tokens with a trailing
    newline; an OAuth token with surrounding whitespace is still an
    OAuth token, and we must not accept it as an API key just because
    the prefix check tripped on the leading space.
    """
    if not isinstance(token, str):
        return False
    return token.strip().startswith(OAUTH_PREFIX)


def is_api_key(token: str) -> bool:
    """Return True iff ``token`` is an Anthropic API key.

    Recognised by the canonical ``sk-ant-api-`` prefix. Same
    whitespace-strip rationale as :func:`is_oauth_token`.
    """
    if not isinstance(token, str):
        return False
    return token.strip().startswith(API_KEY_PREFIX)


def classify(token: str) -> Literal["oauth", "api_key", "unknown"]:
    """Classify ``token`` by its Anthropic credential prefix.

    Returns ``"oauth"`` for the OAuth shape, ``"api_key"`` for the
    API-key shape, ``"unknown"`` for anything else (including empty
    strings, ``None``-coerced inputs, and future Anthropic prefixes
    we don't yet recognise). The three-way return is deliberate:
    callers that want fail-closed semantics ("only accept what we
    explicitly recognise") can check ``== "api_key"``, while callers
    that only want to defend against the known mismap can use
    :func:`assert_api_key`.
    """
    if is_oauth_token(token):
        return "oauth"
    if is_api_key(token):
        return "api_key"
    return "unknown"


def assert_api_key(token: str) -> None:
    """Refuse to treat an OAuth token as an API key.

    Raises :class:`ValueError` with a LOUD, structured operator-facing
    message when ``token`` is an OAuth access token (``sk-ant-oat-…``)
    but a caller is about to inject it as ``ANTHROPIC_API_KEY`` /
    ``SAC_ANTHROPIC_API_KEY``. The error names:

    * the prefix it saw (so the operator can grep their env / logs),
    * the canonical remediation ("use SAC_ANTHROPIC_API_KEY only for
      sk-ant-api-… tokens; the OAuth path is the credentials.json
      bind"),
    * the file the OAuth flow actually uses
      (``~/.claude/.credentials.json`` bind at ``CLAUDE_CONFIG_DIR``).

    Accepts ``sk-ant-api-…`` tokens silently (the intended shape) and
    accepts the ``unknown`` bucket silently too — see :func:`classify`
    for the fail-open rationale. The guard is *narrow*: only the known
    OAuth shape is refused. Unknown shapes are passed through so a
    future Anthropic prefix doesn't brick the fleet on upgrade day.
    """
    kind = classify(token)
    if kind != "oauth":
        return
    # LOUD: name the prefix, name the env var, name the remediation,
    # name the file. The operator must be able to fix this from the
    # message alone, without grepping the codebase for the rule.
    prefix_seen = token.strip()[: len(OAUTH_PREFIX)]
    raise ValueError(
        "Anthropic auth mismap: refusing to inject an OAuth token "
        f"(prefix {prefix_seen!r}) as an API-key env var. "
        "OAuth tokens (sk-ant-oat-…) are NOT valid ANTHROPIC_API_KEY / "
        "SAC_ANTHROPIC_API_KEY values — the Anthropic API rejects them "
        "as 'invalid api key' and the agent goes alive-but-silent. "
        "Use SAC_ANTHROPIC_API_KEY only for sk-ant-api-… (console) "
        "tokens; the OAuth path is the credentials.json bind "
        "(~/.claude/.credentials.json mounted at "
        "CLAUDE_CONFIG_DIR=/tmp/sac-claude). "
        "If you ran `claude login` you have an OAuth token — drop the "
        "ANTHROPIC_API_KEY env and let the credentials.json bind do "
        "the work."
    )
