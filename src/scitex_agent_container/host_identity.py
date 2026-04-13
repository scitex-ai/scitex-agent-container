"""Local host identity detection for runtime-selection fallback (todo#294).

When a fleet-shared YAML declares ``remote: {host: nas, ...}`` and is
launched on the NAS itself, we must not attempt to SSH into ourselves.
This module exposes ``is_local_host(name)`` which returns True when the
given name matches anything the current machine can legitimately be
called.

Sources, in order:
  1. ``socket.gethostname()`` / ``socket.getfqdn()`` / ``os.uname().nodename``
     (both full and short forms).
  2. ``localhost`` / ``127.0.0.1`` / ``::1``.
  3. Env var ``SCITEX_AGENT_LOCAL_HOSTS`` (comma-separated).
  4. ``~/.scitex/agent-container/host_aliases.yaml`` (optional).
  5. Built-in fleet defaults in ``DEFAULT_HOST_ALIASES`` — auto-detects the
     canonical fleet name whose alias list already contains this host.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_HOST_ALIASES: dict[str, list[str]] = {
    "mba": ["mba", "head-mba", "Yusukes-MacBook-Air.local", "localhost"],
    "nas": ["nas", "ugreen", "DXP480TPLUS-994", "nas.local"],
    "spartan": ["spartan", "spartan-login1.hpc.unimelb.edu.au"],
    "ywata-note-win": ["ywata-note-win"],
}

_CACHE: set[str] | None = None


def _short(name: str) -> str:
    return name.split(".", 1)[0] if name else name


def _load_yaml_aliases() -> list[str]:
    path = Path.home() / ".scitex" / "agent-container" / "host_aliases.yaml"
    if not path.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
        if isinstance(data, dict):
            out: list[str] = []
            for v in data.values():
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, list):
                    out.extend(str(x) for x in v)
            return out
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception as exc:
        logger.warning("host_aliases.yaml unreadable, skipping: %s", exc)
    return []


def get_local_identities() -> set[str]:
    """Return the set of names (lower-cased) this host answers to."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    names: set[str] = {"localhost", "127.0.0.1", "::1"}

    try:
        hn = socket.gethostname()
        if hn:
            names.add(hn)
            names.add(_short(hn))
    except Exception:
        hn = ""
    try:
        fqdn = socket.getfqdn()
        if fqdn:
            names.add(fqdn)
            names.add(_short(fqdn))
    except Exception:
        pass
    try:
        nn = os.uname().nodename
        if nn:
            names.add(nn)
            names.add(_short(nn))
    except Exception:
        pass

    env = os.environ.get("SCITEX_AGENT_LOCAL_HOSTS", "")
    for tok in env.split(","):
        tok = tok.strip()
        if tok:
            names.add(tok)

    for alias in _load_yaml_aliases():
        alias = alias.strip()
        if alias:
            names.add(alias)

    # Auto-detect canonical fleet name: if any built-in alias list already
    # contains one of our names, pull in the rest of that list.
    lowered = {n.lower() for n in names}
    for canonical, aliases in DEFAULT_HOST_ALIASES.items():
        alias_lower = {a.lower() for a in aliases}
        if lowered & alias_lower or canonical.lower() in lowered:
            names.update(aliases)
            names.add(canonical)

    _CACHE = {n.lower() for n in names if n}
    return _CACHE


def is_local_host(name: str | None) -> bool:
    """Return True if ``name`` refers to the current host.

    Empty / None / whitespace-only names are treated as local (no remote).
    """
    if name is None:
        return True
    n = name.strip().lower()
    if not n:
        return True
    return n in get_local_identities()


def _reset_cache_for_tests() -> None:
    global _CACHE
    _CACHE = None
