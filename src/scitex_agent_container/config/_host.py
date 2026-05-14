"""Hostname resolution and ${HOSTNAME} substitution for agent YAMLs.

Uses ``${SCITEX_AGENT_CONTAINER_HOSTNAME:-$(hostname -s)}`` as the canonical
hostname (env var wins, short hostname is the fallback). Shared agent
definitions may reference ``${HOSTNAME}`` or ``${SCITEX_AGENT_CONTAINER_HOSTNAME}``
so the same YAML can be launched on every host without drift.

Design constraints:
* Missing vars are a loud error (no silent empty string).
* Substitution happens after YAML parse, before dataclass construction, so
  every string field is covered (metadata labels, env values, hook command
  strings, scheduling.preferred-host, etc.).
* Only ``${HOSTNAME}`` and ``${SCITEX_AGENT_CONTAINER_HOSTNAME}`` are substituted by
  this module — other ``${...}`` placeholders are left alone for downstream
  processors (e.g. MCP interpolation, consumer-defined env resolution) to handle.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Any

from scitex_config._ecosystem import local_state as _local_state

from .._env import getenv as _sac_env

_HOSTNAME_TOKENS = ("HOSTNAME", "SCITEX_AGENT_CONTAINER_HOSTNAME")
_PLACEHOLDER_RE = re.compile(r"\$\{(" + "|".join(_HOSTNAME_TOKENS) + r")\}")


def _config_path() -> Path:
    """Resolve config.yaml via the SciTeX local-state cascade.

    Project-scope (`<repo>/.scitex/agent-container/config.yaml`) wins
    when it exists, else falls back to user-scope under
    ``$SCITEX_DIR/agent-container/`` (default ``~/.scitex/...``). See
    `01_ecosystem_06_local-state-directories.md`.
    """
    return _local_state.path("agent-container", "config.yaml")


def _load_hostname_aliases() -> dict[str, str]:
    """Read ``spec.hostname_aliases`` from ``config.yaml``.

    Returns an empty dict if the file is missing, unparseable, lacks the
    section, or the map isn't a dict. Never raises — hostname resolution
    must still succeed via the identity fallback on a bare host.
    """
    cfg_path = _config_path()
    if not cfg_path.exists():
        return {}
    try:
        import yaml  # PyYAML ships with the container; same import sac uses.
    except (
        ImportError
    ):  # stx-allow: fallback (reason: optional dependency not installed)
        return {}
    # stx-allow: fallback (reason: malformed YAML config must not break hostname resolution; empty aliases dict is the safe default)
    try:
        data = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return {}
    aliases = (data.get("spec") or {}).get("hostname_aliases") or {}
    if not isinstance(aliases, dict):
        return {}
    return {str(k): str(v) for k, v in aliases.items()}


def resolve_hostname(
    gethostname: Callable[[], str] = socket.gethostname,
) -> str:
    """Return the canonical host label for this machine.

    Resolution order (first non-empty wins):
      1. ``SCITEX_AGENT_CONTAINER_HOSTNAME`` env var (manual override).
      2. ``SCITEX_AGENT_CONTAINER_HOSTNAME`` env var.
      3. ``hostname_aliases[short hostname]`` from
         ``shared/config.yaml`` or ``~/.scitex/agent-container/config.yaml``.
      4. ``socket.gethostname()`` short form (identity fallback).

    Args:
        gethostname: Callable returning the raw OS hostname. Defaults to
            ``socket.gethostname`` (production). Tests inject a callable
            returning a fixed string instead of patching ``socket``.

    Raises:
        RuntimeError: If none of the sources produces a non-empty value. This
            should be practically impossible (``gethostname()`` returns
            something on any configured box) but is handled loudly rather
            than returning the empty string.
    """
    env = _sac_env("HOSTNAME", "").strip()
    if env:
        return env
    env = _sac_env("HOSTNAME", "").strip()
    if env:
        return env
    hn = gethostname()
    short = hn.split(".", 1)[0] if hn else ""
    aliases = _load_hostname_aliases()
    if short and short in aliases:
        return aliases[short]
    if short:
        return short
    raise RuntimeError(
        "Cannot resolve hostname: SCITEX_AGENT_CONTAINER_HOSTNAME and "
        "SCITEX_AGENT_CONTAINER_HOSTNAME unset, socket.gethostname() empty, "
        "no config.yaml alias applicable."
    )


def _substitute_string(value: str, hostname: str) -> str:
    """Replace ${HOSTNAME} / ${SCITEX_AGENT_CONTAINER_HOSTNAME} occurrences in a string.

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
