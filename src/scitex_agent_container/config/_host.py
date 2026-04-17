"""Hostname resolution and ${HOSTNAME} substitution for agent YAMLs.

The fleet uses ``${SCITEX_OROCHI_HOSTNAME:-$(hostname -s)}`` as the canonical
hostname (env var wins, short hostname is the fallback). Shared agent
definitions under ``shared/agents/`` may reference ``${HOSTNAME}`` or
``${SCITEX_OROCHI_HOSTNAME}`` so the same YAML can be launched on every
host without drift.

Design constraints:
* Missing vars are a loud error (no silent empty string).
* Substitution happens after YAML parse, before dataclass construction, so
  every string field is covered (metadata labels, env values, hook command
  strings, scheduling.preferred-host, etc.).
* Only ``${HOSTNAME}`` and ``${SCITEX_OROCHI_HOSTNAME}`` are substituted by
  this module — other ``${...}`` placeholders (e.g. ``${SCITEX_OROCHI_TOKEN}``
  handled by mcp interpolation) are left alone.
"""

from __future__ import annotations

import os
import re
import socket
from typing import Any

_HOSTNAME_TOKENS = ("HOSTNAME", "SCITEX_OROCHI_HOSTNAME")
# Match ${VAR} exactly (no :- default expressions — we resolve hostname
# ourselves via resolve_hostname()).
_PLACEHOLDER_RE = re.compile(r"\$\{(" + "|".join(_HOSTNAME_TOKENS) + r")\}")


def resolve_hostname() -> str:
    """Return the canonical hostname for this host.

    Resolution order (first non-empty wins):
      1. ``SCITEX_OROCHI_HOSTNAME`` env var.
      2. ``socket.gethostname()`` short form (first dot-separated component).

    Raises:
        RuntimeError: If neither source produces a non-empty value. This
            should be practically impossible (``gethostname()`` returns
            something on any configured box) but is handled loudly rather
            than returning the empty string.
    """
    env = os.environ.get("SCITEX_OROCHI_HOSTNAME", "").strip()
    if env:
        return env
    hn = socket.gethostname()
    if hn:
        return hn.split(".", 1)[0]
    raise RuntimeError(
        "Cannot resolve hostname: SCITEX_OROCHI_HOSTNAME unset and "
        "socket.gethostname() returned empty."
    )


def _substitute_string(value: str, hostname: str) -> str:
    """Replace ${HOSTNAME} / ${SCITEX_OROCHI_HOSTNAME} in a string.

    Other ``${...}`` placeholders are preserved as-is so downstream code
    (e.g. mcp interpolation) keeps working.
    """

    def _repl(match: "re.Match[str]") -> str:
        # hostname always resolves — resolve_hostname() has already succeeded
        # or raised. The placeholder set is closed, so we don't need to
        # handle "missing var" inside the callback.
        return hostname

    return _PLACEHOLDER_RE.sub(_repl, value)


def substitute_hostnames(obj: Any, hostname: str | None = None) -> Any:
    """Recursively walk a dict/list/str and substitute hostname placeholders.

    Non-string leaves (int, bool, None) are returned unchanged. The walk is
    pure-functional — the input is not mutated; a new structure is returned.

    Args:
        obj: YAML-parsed structure (dict/list/scalar).
        hostname: Override hostname (for tests). If None, calls
            ``resolve_hostname()``.
    """
    if hostname is None:
        hostname = resolve_hostname()

    if isinstance(obj, str):
        return _substitute_string(obj, hostname)
    if isinstance(obj, dict):
        return {k: substitute_hostnames(v, hostname) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_hostnames(item, hostname) for item in obj]
    return obj
