"""The write-time refusal: a credential ROW may never carry credential MATERIAL.

Why this module exists rather than a code-review convention
-----------------------------------------------------------
:mod:`.._account._keepalive_guards` already proves the pattern works. Its
``assert_access_only`` re-scans a payload for refresh material at every
depth *even though* ``mint_access_only_artifact`` strips it by
construction, on the stated grounds that "a guard that only runs when the
stripper is correct guards nothing".

That guard sits on ONE rail — the ssh keepalive push. A credential table
opens a SECOND rail, and it is a quieter one: ``assert_access_only``
raises; ``INSERT`` does not. Worse, the rows in this domain are designed
to REPLICATE (ADR-0022 §5), so a secret that reaches a row does not reach
one place, it reaches every place the row is ever pulled to, plus each of
their WALs and base backups, where rotation cannot retract it.

So the same guard is re-erected on the new rail, in the same shape and
for the same reason.

What counts as material
-----------------------
Two independent tests, because either alone is escapable:

1. **By key name** — the recursive key scan, widened past
   ``refresh_token`` to every field name that means "the secret itself".
   Catches a well-named field carrying the wrong thing.
2. **By value shape** — high-entropy runs, provider-specific prefixes and
   the structural forms real credentials take (JWT, PEM, ``sk-ant-``,
   Telegram ``<digits>:<35+>``). Catches a badly-named field, which is
   the realistic case: nobody adds a column called ``refresh_token``,
   they paste a token into ``note``.

Nothing here ever returns, logs or re-raises the offending value. The
error names the FIELD and the reason, never the content — an exception
string is exactly the kind of thing that ends up in a transcript.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


class CredentialMaterialError(ValueError):
    """A descriptor row carries something secret-shaped. The write is REFUSED.

    Names the offending field path and why it tripped. NEVER carries the
    value — this exception is expected to reach logs and transcripts.
    """


#: Field names that mean "the secret itself", at any nesting depth.
#: Superset of ``_keepalive_guards._REFRESH_KEYS``: this rail records
#: descriptors for every credential kind in the fleet, not only OAuth.
_MATERIAL_KEYS: frozenset[str] = frozenset(
    {
        "refreshtoken",
        "refresh_token",
        "accesstoken",
        "access_token",
        "idtoken",
        "id_token",
        "token",
        "secret",
        "password",
        "passwd",
        "apikey",
        "api_key",
        "client_secret",
        "clientsecret",
        "private_key",
        "privatekey",
        "bot_token",
        "bottoken",
        "session_key",
        "sessionkey",
        "credentials",
    }
)

#: Fields whose job is to name WHERE material lives or HOW to renew it.
#: They legitimately hold long unbroken strings — a path, a URL, a
#: command — so the generic high-entropy heuristic is waived for them.
#: Every explicit provider pattern still applies: a locator may say
#: ``env:ANTHROPIC_API_KEY`` but must never contain an ``sk-ant-`` value.
_LOCATOR_KEYS: frozenset[str] = frozenset(
    {"locator", "obtain_command", "refresh_command"}
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "an Anthropic key/token prefix",
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    ),
    (
        "an OpenAI key prefix",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}"),
    ),
    (
        "a GitHub/forge token prefix",
        re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{16,}"),
    ),
    (
        "a Slack token prefix",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ),
    (
        "a JWT (three base64url segments)",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"),
    ),
    (
        "a PEM private-key block",
        re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----"),
    ),
    (
        "a Telegram bot-token shape (<digits>:<35+ chars>)",
        re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}"),
    ),
    (
        "an OAuth refresh-token prefix",
        re.compile(r"\bsk-ant-ort01-[A-Za-z0-9_\-]{8,}"),
    ),
)

#: A bare high-entropy run with no recognisable prefix. Deliberately
#: conservative (>=40 chars of unbroken secret alphabet): descriptors
#: legitimately hold uuids (36 chars, hyphen-broken into short groups),
#: hostnames and paths, and a rule that flags those is a rule people
#: disable.
_ENTROPY_RUN = re.compile(r"[A-Za-z0-9+/_\-]{40,}")

#: Value shapes that match ``_ENTROPY_RUN`` but are legitimate content.
_ENTROPY_EXEMPT = (
    re.compile(r"^[0-9a-fA-F-]{36}$"),          # uuid
    re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_.\-]+$"),  # dotted names
)


def _value_offence(value: str, *, allow_entropy: bool = False) -> str | None:
    """Return why ``value`` looks like material, or None. Never echoes it.

    ``allow_entropy`` waives only the generic high-entropy heuristic, for
    the locator/command fields whose legitimate content is a long path or
    command line. Provider-specific patterns are never waived.
    """
    for reason, pattern in _PATTERNS:
        if pattern.search(value):
            return reason
    if allow_entropy:
        return None
    for exempt in _ENTROPY_EXEMPT:
        if exempt.match(value.strip()):
            return None
    run = _ENTROPY_RUN.search(value)
    if run is not None and "/" not in run.group(0):
        return (
            f"an unbroken {len(run.group(0))}-character high-entropy run, "
            "which is the shape of a raw key"
        )
    return None


def find_material(
    payload: Any, *, path: str = "", allow_entropy: bool = False
) -> list[str]:
    """Dotted paths of every field that carries or names credential material.

    Recursive for the same reason ``find_refresh_keys`` is: the guard must
    not depend on the payload's nesting shape, because the shape is
    exactly what varies between credential dialects.

    Each entry is ``"<path>: <reason>"``. Reasons never quote the value.
    """
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            key_l = str(key).lower()
            if key_l in _MATERIAL_KEYS:
                found.append(
                    f"{here}: the field NAME means credential material"
                )
            found.extend(
                find_material(
                    value, path=here, allow_entropy=key_l in _LOCATOR_KEYS
                )
            )
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found.extend(
                find_material(
                    value, path=f"{path}[{index}]", allow_entropy=allow_entropy
                )
            )
    elif isinstance(payload, str):
        offence = _value_offence(payload, allow_entropy=allow_entropy)
        if offence is not None:
            found.append(f"{path or '<value>'}: the VALUE looks like {offence}")
    return found


def assert_no_material(payload: Any, *, what: str) -> None:
    """Refuse ``payload`` if anything in it is secret-shaped. Fail loud.

    ``what`` names the row being written (e.g. ``"descriptor
    anthropic-oauth:ywatanabe@scitex.ai"``) so the operator can find it
    without the message quoting a byte of it.
    """
    offenders = find_material(payload)
    if offenders:
        joined = "; ".join(sorted(offenders))
        raise CredentialMaterialError(
            f"refusing to record {what}: the row carries credential "
            f"material at {joined}. This store holds FACTS about "
            "credentials — which account, which host is primary, when it "
            "expires, where it lives — and never the material itself. "
            "These rows are designed to replicate (ADR-0022 §5), so a "
            "secret written here is a secret in every store it reaches "
            "plus their WALs and base backups, where rotation cannot "
            "retract it. Record a locator instead. Nothing was written."
        )
