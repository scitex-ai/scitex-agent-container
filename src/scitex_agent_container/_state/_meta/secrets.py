"""Secret-redaction helpers for ``agent_meta.collect_rich``.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports ``_SECRET_PATTERNS`` and
``_redact_secrets`` so existing callers (including tests using
``agent_meta._redact_secrets``) continue to work unchanged.

Also home to :func:`_redact_env_entry` (+ its ``_SECRET_ENV`` key-name
regex) — the ``KEY=value`` argv-element redactor originally written
for ``cli_pkg._explain``'s console plan-preview. Centralised here
(rather than left in ``cli_pkg``) so runtime modules that must redact
an on-disk argv/env record (``_apptainer_runtime.py``,
``tui_session.py`` — see card ``sac-argv-token-plaintext``) can reuse
the exact same secret-name matching without a ``runtimes`` →
``cli_pkg`` import (which would invert the package's dependency
direction). ``cli_pkg._explain`` re-exports both names for back-compat.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS = [
    re.compile(r"(sk-ant-[A-Za-z0-9_-]+)"),
    re.compile(r"(wks_[A-Za-z0-9]+)"),
    re.compile(
        r"((?:token|secret|api[_-]?key|password|bearer)\s*[=:]\s*)(\S+)",
        re.IGNORECASE,
    ),
]


def _redact_secrets(text: str) -> str:
    if not text:
        return ""
    s = text
    for pat in _SECRET_PATTERNS:
        if pat.groups == 2:
            s = pat.sub(lambda m: m.group(1) + "***REDACTED***", s)
        else:
            s = pat.sub("***REDACTED***", s)
    return s


# Matches a secret-shaped env-var KEY (not its value) — deliberately
# generic so it covers any ``*_API_KEY`` / ``*_TOKEN`` / ``*_BEARER`` /
# ``*_KEY`` / ``*_CREDENTIAL`` / ``*_SECRET`` / ``*_PASSWORD`` name, not
# just ``SAC_ANTHROPIC_API_KEY`` (the incident var).
_SECRET_ENV = re.compile(
    r"(SECRET|TOKEN|BEARER|PASSWORD|API_KEY|_KEY|CREDENTIAL)", re.IGNORECASE
)


def _redact_env_entry(entry: str) -> str:
    """Mask the VALUE of a secret-named ``KEY=value`` argv/env entry.

    Never touches entries that aren't ``KEY=value`` shaped (plain flags
    like ``--bind`` or ``--env`` pass through untouched) and never
    touches a non-secret-named key (e.g. ``CLAUDE_AGENT_ID=...``). Used
    both for the human-facing ``sac agents explain`` plan preview and
    for on-disk argv/plan snapshots (``apptainer_run.argv.txt``) so the
    two surfaces apply IDENTICAL redaction and never drift.
    """
    key, sep, val = entry.partition("=")
    if sep and val and _SECRET_ENV.search(key):
        return f"{key}=<redacted: {len(val)} chars>"
    return entry
