"""Observe native uvicorn signal dispositions in a disposable child process."""

import asyncio
import signal
import socket

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from scitex_agent_container._runners._a2a_proxy_server import build_server


async def observe_signal_ownership():
    """Serve actual HTTP; compare real dispositions before/during/after serving."""
    signals = (signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.getsignal(sig) for sig in signals}
    received = []

    def owned_handler(signum, _frame):
        received.append(signum)

    async def health(_request):
        return JSONResponse({"status": "ok"})

    for sig in signals:
        signal.signal(sig, owned_handler)
    server = build_server(
        uvicorn.Config(
            Starlette(routes=[Route("/health", health)]),
            log_level="error",
            lifespan="off",
            ws="none",
        )
    )
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(2):
                    while not server.started:
                        await asyncio.sleep(0.01)
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                during = [signal.getsignal(sig) is owned_handler for sig in signals]
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=2)
        after = [signal.getsignal(sig) is owned_handler for sig in signals]
        for sig in signals:
            signal.raise_signal(sig)
        return {
            "health": response.json(),
            "during": during,
            "after": after,
            "received": received,
        }
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
