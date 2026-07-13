"""Host-DISPLAY resolution for the ``sac agents list`` Host column.

Split out of ``_agent_list.py`` (already at the 512-line per-file cap) so
the tiny host-display concern lives next to its siblings
``_agent_list_account`` (Account column) and ``_agent_list_render`` (table).

``host: local`` was banned on the spec-INPUT side (operator 2026-07-10;
the ``${HOSTNAME}`` placeholder replaced it), but list rows still carry the
``"local"`` sentinel in their ``host`` field. These helpers resolve the
machine's canonical hostname for DISPLAY while leaving the raw ``host``
sentinel intact for backward-compat consumers (``_is_ghost_row`` keys on
``host == "local"`` to recognise a LOCAL agent).
"""

from __future__ import annotations


def _resolve_display_host() -> str:
    """Resolve THIS machine's canonical hostname for the Host DISPLAY column.

    Uses the SAME resolver ``${HOSTNAME}`` expands through —
    :func:`config._host.resolve_hostname` (``SCITEX_AGENT_CONTAINER_HOSTNAME``
    env → ``config.yaml`` alias → ``hostname -s``) — so the displayed Host
    (e.g. ``ywata-note-win``) matches what a freshly-created spec records.
    Tolerant: any resolution failure degrades to the socket short-name, then
    to ``"local"``, so the list command never crashes on a hostname hiccup.
    """
    # stx-allow: fallback (reason: hostname resolution must never crash the
    # list command; degrade to the socket short-name, then the raw sentinel.)
    try:
        from ...config._host import resolve_hostname

        return resolve_hostname()
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        import socket

        return socket.gethostname().split(".")[0] or "local"


def _host_display_for(host_raw: str | None, resolved: str) -> str:
    """Map a row's raw ``host`` to its DISPLAY value.

    The ``"local"`` / ``"localhost"`` / empty sentinels resolve to the
    machine's canonical hostname (``resolved``); any concrete host label
    passes through unchanged (forward-safe if a real host is ever set).
    """
    if host_raw in ("local", "localhost", "", None):
        return resolved
    return host_raw
