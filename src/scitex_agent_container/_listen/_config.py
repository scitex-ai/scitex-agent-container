"""Sac-listen runtime configuration helpers.

Centralises the read of ``listen.port`` / ``listen.host`` from
``~/.scitex/agent-container/config.yaml`` (or
``$SCITEX_AGENT_CONTAINER_CONFIG``) so that every caller that needs to
build "the URL clients use to reach sac listen" agrees on one answer.

Used by:

* :mod:`scitex_agent_container.runtimes._apptainer_runtime` — injects
  the resolved ``SAC_LISTEN_BASE_URL`` env var into every container so
  the per-agent sidecar can advertise the host-stable URL on its
  AgentCard (rather than its own internal port, which churns under
  auto-allocation).
* :mod:`scitex_agent_container._runners._session_http` — when the
  ``SAC_LISTEN_BASE_URL`` env var is set inside the container, the
  ``/.well-known/agent-card.json`` handler uses it as ``base_url``
  instead of the volatile ``request.base_url``.

Everything here is tolerant: a missing / malformed config falls back
to the built-in default (``http://127.0.0.1:7878``). Agent start-up
must never block on a broken operator config.
"""

from __future__ import annotations

DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 7878


def _read_listen_block() -> dict:
    """Return the ``listen:`` block from config.yaml, or ``{}`` if absent.

    Tolerant: missing file, unparseable yaml, non-mapping ``listen:``
    all yield ``{}`` (caller then falls back to defaults).
    """
    # stx-allow: fallback (reason: config.yaml is operator-edited and
    # may be malformed; a broken listen block must not block agent
    # start-up — fall back to the built-in default.)
    try:
        from .._state.host_config import _default_config_path

        path = _default_config_path()
        if not path.is_file():
            return {}
        import yaml

        raw = yaml.safe_load(path.read_text()) or {}
        listen_raw = raw.get("listen") if isinstance(raw, dict) else None
        return listen_raw if isinstance(listen_raw, dict) else {}
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return {}


def listen_host() -> str:
    """Resolve the host clients should target to reach ``sac listen``.

    Precedence: ``listen.host`` in config.yaml >
    :data:`DEFAULT_LISTEN_HOST` (``127.0.0.1``). The host is never read
    from env — env-level overrides are the listener's own concern, not
    the URL we advertise to peers.
    """
    block = _read_listen_block()
    host = block.get("host")
    if isinstance(host, str) and host.strip():
        return host.strip()
    return DEFAULT_LISTEN_HOST


def listen_port() -> int:
    """Resolve the port clients should target to reach ``sac listen``.

    Precedence: ``listen.port`` in config.yaml >
    :data:`DEFAULT_LISTEN_PORT` (``7878``). Integer-coerced; non-int
    or non-positive values silently fall back to the default.
    """
    block = _read_listen_block()
    port = block.get("port")
    if isinstance(port, int) and port > 0:
        return port
    if isinstance(port, str) and port.strip().isdigit():
        coerced = int(port.strip())
        if coerced > 0:
            return coerced
    return DEFAULT_LISTEN_PORT


def listen_base_url() -> str:
    """Return the stable URL that reaches ``sac listen`` from outside.

    Shape: ``http://<host>:<port>`` (no trailing slash, no path
    suffix). Combine with ``/agents/<name>`` to build a card's
    ``url`` field that survives per-agent port churn.
    """
    return f"http://{listen_host()}:{listen_port()}"


def container_reachable_host() -> str:
    """Resolve the host a *container* must dial to reach ``sac listen``.

    Unlike :func:`listen_host` (which defaults to the loopback
    ``127.0.0.1`` the server binds), this returns a host name that is
    reachable from *inside* an agent container — the same canonical
    hostname the a2a turn_url path uses
    (:func:`_listen._registry_endpoints._instance_or_local_host` →
    ``host_config.canonical_host()``). This is the root-cause fix for
    "agent inside a sac container cannot spawn peers": injecting
    ``SAC_LISTEN_BASE_URL=http://127.0.0.1:7878`` points the spawn
    client at the *container's own* loopback (nothing listens there),
    whereas a2a already reaches the host via ``hostname:port``.

    Precedence:
      1. ``listen.host`` in config.yaml when it is an explicit,
         NON-loopback address (an operator who set a tailscale/tunnel
         host means it — honour it verbatim).
      2. ``host_config.canonical_host()`` — the hostname every other
         state.db write and a2a turn_url already uses for self-identity.
      3. :func:`listen_host` (the loopback default) as the last-ditch
         fallback so a broken host_config never blocks startup.
    """
    configured = listen_host()
    if configured not in ("127.0.0.1", "localhost", "::1"):
        return configured
    # stx-allow: fallback (reason: host_config read may fail in a
    # restricted environment; the loopback default is a safe baseline
    # and must never block agent start-up.)
    try:
        from .._state.host_config import load as load_host_config

        canonical = (load_host_config().canonical_host() or "").strip()
        if canonical and canonical not in ("127.0.0.1", "localhost", "::1"):
            return canonical
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass
    return configured


def container_listen_base_url() -> str:
    """``http://<container_reachable_host>:<port>`` for ``SAC_LISTEN_BASE_URL``.

    The value injected into agent containers so their spawn / start /
    ACL clients dial a host-REACHABLE address (mirroring a2a) rather
    than the container-local loopback.
    """
    return f"http://{container_reachable_host()}:{listen_port()}"


__all__ = [
    "DEFAULT_LISTEN_HOST",
    "DEFAULT_LISTEN_PORT",
    "listen_host",
    "listen_port",
    "listen_base_url",
    "container_reachable_host",
    "container_listen_base_url",
]
