"""Secret-redaction helpers for ``agent_meta.collect_rich``.

Extracted from ``agent_meta.py`` to keep that module under the 512-line
hook ceiling. ``agent_meta`` re-exports ``_SECRET_PATTERNS`` and
``_redact_secrets`` so existing callers (including tests using
``agent_meta._redact_secrets``) continue to work unchanged.
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
