"""Foreground A2A serving and optional validated socket activation."""

import socket
from pathlib import Path

import scitex_logging as slogging

from ._listener import _validate_listener

log = slogging.getLogger("scitex_agent_container.a2a._server")


def serve(
    agent_yamls: list[Path],
    *,
    host: str = "127.0.0.1",
    port: int = 8888,  # stx-allow: STX-NL001
    handler: str = "echo",
    fd: int | None = None,
) -> None:
    """Run the A2A HTTP server in the foreground via uvicorn.

    ``handler`` is the *default* selector — agents whose yaml sets
    ``spec.a2a.handler: <key>`` override it. ``fd`` optionally activates a
    POSIX listener already bound to exactly ``host`` and ``port``; it is not
    rebound. Its caller retains ownership of the original descriptor.
    """
    from ._server import build_app

    if fd is not None:
        _validate_listener(fd, host, port)
    try:
        import uvicorn
    except Exception as exc:  # pragma: no cover  # stx-allow: fallback (reason: optional dependency not installed)
        # Broaden: uvicorn import can fail with non-ImportError if a
        # transitive dep (httptools, websockets) is mis-built. Surface
        # any such failure as an actionable ImportError.
        raise ImportError(
            "uvicorn is required to run 'sac a2a serve'; install with "
            "'pip install uvicorn'."
        ) from exc

    base_url = f"http://{host}:{port}"
    app = build_app(
        agent_yamls,
        default_handler=handler,
        base_url=base_url,
        listen_port=port,
    )

    log.info(
        "sac-a2a (a2a-sdk) listening on http://%s:%d (default handler: %s)",
        host,
        port,
        handler,
    )
    socket.setdefaulttimeout(60)
    # ``ws="none"`` — A2A is HTTP/JSON-RPC + SSE only. Skipping the
    # websockets protocol avoids the uvicorn 0.27 ↔ websockets 15
    # incompatibility (``websockets.legacy`` removed in 14+).
    options = {} if fd is None else {"fd": fd}
    uvicorn.run(app, host=host, port=port, log_level="info", ws="none", **options)
