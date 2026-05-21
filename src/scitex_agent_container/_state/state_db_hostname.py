"""Canonical-hostname resolution for state.db writes (F-CS12).

Extracted from :mod:`state_db` so that module stays under the per-file
line cap. :func:`resolve_host` answers "what host string do I stamp on
this instances row?" and degrades gracefully to a bare hostname when
config.yaml is missing or malformed (a config error must never block a
state.db write).
"""

from __future__ import annotations

from .._env import getenv as _sac_env


def resolve_host(host: str | None) -> str:
    """Canonical hostname for state.db writes.

    Resolution chain (F-CS12):
        1. ``host`` arg (explicit override)
        2. ``$SAC_HOST`` env var
        3. ``host.canonical`` from config.yaml
        4. ``host.aliases[$(hostname -s)]`` from config.yaml
        5. ``$(hostname -s)`` (or fqdn when fallback=hostname-fqdn)
    """
    if host:
        return host
    from . import host_config

    # stx-allow: fallback (reason: a malformed config.yaml must not block
    # state.db writes — degrade to hostname-only resolution.)
    try:
        return host_config.load().canonical_host()
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        import socket

        return _sac_env("HOST") or socket.gethostname().split(".")[0]
