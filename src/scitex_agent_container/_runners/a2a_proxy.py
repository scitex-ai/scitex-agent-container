"""Runner for ``kind: AgentProxy`` agents — HTTP forwarder, no SDK.

Mirrors the lifecycle shape of :mod:`._runners.claude_session` (pid /
heartbeat / signal handling) but the body is a Starlette + uvicorn
server that forwards POST /v1/turn to an external A2A endpoint.

Routes:

* ``POST /v1/turn``      Forward to ``<upstream-base>/v1/turn``.
                          Reject (400) on redact-token hit; surface
                          upstream timeouts as 504, upstream 5xx as
                          502. Refuse cross-host redirects.
* ``GET  /health``        Liveness — ``{status, upstream, trust}``.
* ``GET  /.well-known/agent-card.json``  Spliced AgentCard — upstream
                          skills/capabilities/provider preserved; our
                          name/url + ``x-scitex-agent-container.{kind,
                          upstream, trust}`` overlaid.

Invocation::

    python -m scitex_agent_container._runners.a2a_proxy \\
        --name <agent> --upstream <url> [--trust ...] [--redact a,b]
        [--timeout-s 30] [--a2a-port 7901] [--a2a-host 127.0.0.1]
        [--a2a-card-yaml /path/to/spec.yaml]

The state-dir layout (pid + heartbeat.json) mirrors claude_session so
``sac agent status`` works against either runner uniformly.

Egress lockdown (Layer 5 of the proxy roadmap, rolled into Layer 3):
the only outbound destination we honor is the upstream's host. Any
3xx redirect to a different host is rejected at the application
layer with HTTP 502 — apptainer ``--net`` lockdown isn't needed for
the MVP.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ._session_state import (
    DEFAULT_STATE_ROOT,
    DEFAULT_TICK_SECONDS,
    STATE_STARTING,
    STATE_STOPPING,
    state_dir_for,
    write_heartbeat,
    write_pid,
)
from ._session_state import (
    heartbeat_loop as _heartbeat_loop,
)

logger = logging.getLogger(__name__)

__all__ = ["main", "run", "build_app", "splice_card"]


# ---------------------------------------------------------------------------
# Card splicing
# ---------------------------------------------------------------------------


def splice_card(
    upstream_card: dict[str, Any] | None,
    *,
    name: str,
    our_url: str,
    upstream: str,
    trust: str,
    fetch_error: str = "",
) -> dict[str, Any]:
    """Return an A2A v1-shaped AgentCard built from upstream's card + our overrides.

    Preserves upstream's skills / capabilities / provider / default*Modes
    and any non-v0 fields. Overrides:

      * ``name``  ->  our ``name``
      * ``supportedInterfaces``  ->  ``[{url: our_url, protocolBinding:
                                     "HTTP+JSON", tenant: name,
                                     protocolVersion: "1.0"}]``
      * ``x-scitex-agent-container``  ->  block describing the proxy
                                          (kind, upstream, trust;
                                          optionally upstream_card_fetch_error)

    A2A v0-shape fields (``url``, ``authentication``,
    ``stateTransitionHistory``) on the upstream card are dropped — any
    A2A v1 client validating via ``ParseDict(card, AgentCard())`` would
    reject them otherwise. See ``a2a/_card.py::build_card`` for the
    canonical v1 shape.

    If ``upstream_card`` is ``None`` (boot-time fetch failed), serve a
    minimal v1 card with our overrides + the fetch error surfaced under
    ``x-scitex-agent-container.upstream_card_fetch_error``.
    """
    base: dict[str, Any] = dict(upstream_card or {})
    # Drop A2A v0-shape top-level fields that v1 ParseDict would reject.
    for v0_field in ("url", "authentication", "stateTransitionHistory"):
        base.pop(v0_field, None)

    base["name"] = name
    base["supportedInterfaces"] = [
        {
            "url": our_url,
            "protocolBinding": "HTTP+JSON",
            "tenant": name,
            "protocolVersion": "1.0",
        }
    ]

    sx: dict[str, Any] = {
        "kind": "AgentProxy",
        "upstream": upstream,
        "trust": trust,
    }
    if fetch_error:
        sx["upstream_card_fetch_error"] = fetch_error
    base["x-scitex-agent-container"] = sx
    return base


# ---------------------------------------------------------------------------
# Starlette app (factored for tests)
# ---------------------------------------------------------------------------


def _upstream_base(upstream: str) -> str:
    """Return the base URL (drop ``/.well-known/agent-card.json`` tail if any)."""
    if upstream.endswith("/.well-known/agent-card.json"):
        return upstream[: -len("/.well-known/agent-card.json")]
    if upstream.endswith("/.well-known/agent.json"):
        return upstream[: -len("/.well-known/agent.json")]
    return upstream.rstrip("/")


def _upstream_host(upstream: str) -> str:
    return urlparse(_upstream_base(upstream)).netloc


def build_app(
    *,
    name: str,
    upstream: str,
    trust: str,
    redact: list[str],
    timeout_s: float,
    upstream_card: dict[str, Any] | None,
    upstream_card_error: str = "",
    httpx_client: Any | None = None,
) -> Any:
    """Build the Starlette app. Factored out so tests can mount it directly.

    ``httpx_client`` is an injected ``httpx.AsyncClient`` (for tests with
    an in-process upstream). If ``None``, each request opens a fresh
    client — cheap enough for the MVP, no connection pooling tuning yet.
    """
    import httpx
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    base = _upstream_base(upstream)
    upstream_netloc = _upstream_host(upstream)

    async def _forward(body: dict) -> Any:
        client = httpx_client or httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=False
        )
        try:
            resp = await client.post(f"{base}/v1/turn", json=body)
        finally:
            if httpx_client is None:
                await client.aclose()
        return resp

    async def post_turn(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError as exc:
            return JSONResponse({"error": f"bad JSON: {exc}"}, status_code=400)

        text = body.get("text") if isinstance(body, dict) else None
        if isinstance(text, str) and redact:
            for token in redact:
                if token and token in text:
                    return JSONResponse(
                        {
                            "error": (
                                f"refused: prompt contains redacted "
                                f"substring (proxy trust={trust})"
                            )
                        },
                        status_code=400,
                    )

        try:
            resp = await _forward(body)
        except httpx.TimeoutException:  # stx-allow: fallback (reason: surface upstream timeout as 504 instead of crashing)
            return JSONResponse(
                {"error": f"upstream timeout after {timeout_s:.0f}s"},
                status_code=504,
            )
        except httpx.HTTPError as exc:  # stx-allow: fallback (reason: any network-layer error from upstream surfaces as 502 to caller)
            return JSONResponse(
                {"error": f"upstream unreachable: {exc}"}, status_code=502
            )

        # Egress lockdown — refuse 3xx that would re-target a different
        # host. The application-layer redirect refusal IS our lockdown
        # for the MVP (apptainer --net path is the next rabbit hole).
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location", "")
            target_host = urlparse(location).netloc if location else ""
            if target_host and target_host != upstream_netloc:
                return JSONResponse(
                    {
                        "error": (
                            "upstream redirected to disallowed host "
                            f"'{target_host}' (proxy is locked to "
                            f"'{upstream_netloc}')"
                        )
                    },
                    status_code=502,
                )

        if 500 <= resp.status_code < 600:
            return JSONResponse(
                {"error": f"upstream {resp.status_code}: {resp.text}"},
                status_code=502,
            )

        try:
            payload = resp.json()
        except ValueError:
            payload = {"reply": resp.text}
        return JSONResponse(payload, status_code=resp.status_code)

    async def get_health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "upstream": upstream, "trust": trust})

    async def get_agent_card(request: Request) -> JSONResponse:
        base_url = str(request.base_url).rstrip("/")
        our_url = f"{base_url}/agents/{name}"
        card = splice_card(
            upstream_card,
            name=name,
            our_url=our_url,
            upstream=upstream,
            trust=trust,
            fetch_error=upstream_card_error,
        )
        return JSONResponse(card)

    routes = [
        Route("/v1/turn", post_turn, methods=["POST"]),
        Route("/health", get_health, methods=["GET"]),
        Route("/.well-known/agent-card.json", get_agent_card, methods=["GET"]),
        Route("/.well-known/agent.json", get_agent_card, methods=["GET"]),
    ]
    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# Upstream card boot-time fetch
# ---------------------------------------------------------------------------


async def _fetch_upstream_card(
    upstream: str, *, timeout_s: float
) -> tuple[dict[str, Any] | None, str]:
    """Fetch ``<upstream-base>/.well-known/agent-card.json`` once at boot.

    Returns ``(card_dict, "")`` on success or ``(None, error_msg)`` on
    failure — the proxy stays up either way; the failure is surfaced
    on the spliced card so operators see *why* the upstream skills
    aren't propagating.
    """
    import httpx

    base = _upstream_base(upstream)
    url = f"{base}/.well-known/agent-card.json"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json(), ""
    except Exception as exc:  # stx-allow: fallback (reason: card fetch failure is non-fatal; surfaced on the proxy's card instead)
        return None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def run(
    name: str,
    *,
    upstream: str,
    trust: str = "untrusted",
    redact: list[str] | None = None,
    timeout_s: float = 30.0,
    state_root: Path | None = None,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    a2a_host: str = "127.0.0.1",
    a2a_port: int | None = None,
    a2a_card_yaml: str = "",
    stop_event: asyncio.Event | None = None,
) -> int:
    """Run the proxy daemon until SIGTERM / SIGINT (or ``stop_event``).

    ``stop_event``: optional, in-process shutdown signal. When set,
    ``run()`` shuts down identically to receiving SIGTERM but without
    requiring the caller to send the signal. In-process drivers
    (tests, supervisor harnesses) should prefer this over
    ``os.kill(getpid(), SIGTERM)`` — the latter triggers global signal
    handlers (e.g. uvicorn's ``handle_exit``) whose side effects leak
    into whatever runs after this coroutine returns.
    """
    del a2a_card_yaml  # reserved for future per-agent card overrides

    redact = list(redact or [])
    state_dir = state_dir_for(name, state_root)
    state_dir.mkdir(parents=True, exist_ok=True)

    pid = os.getpid()
    write_pid(state_dir, pid)
    write_heartbeat(state_dir, pid=pid, state=STATE_STARTING)

    stop = stop_event if stop_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(signum: int) -> None:
        logger.info("a2a_proxy %s received signal %d", name, signum)
        write_heartbeat(state_dir, pid=pid, state=STATE_STOPPING)
        stop.set()

    # Track which signals we touched so the finally-block can restore
    # them. Without this, ``run()`` leaks SIGTERM/SIGINT dispositions
    # into whatever runs after — important for in-process callers
    # (tests, supervisor harnesses) that drive run() and continue.
    asyncio_handlers: list[int] = []
    fallback_handlers: dict[int, Any] = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
            asyncio_handlers.append(sig)
        except (
            NotImplementedError
        ):  # stx-allow: fallback (reason: Windows / no asyncio signal support)
            fallback_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, lambda s, _f: _on_signal(s))

    hb_task = asyncio.create_task(
        _heartbeat_loop(state_dir, pid=pid, tick_seconds=tick_seconds, stop=stop)
    )

    upstream_card, fetch_err = await _fetch_upstream_card(upstream, timeout_s=timeout_s)

    serve_task: asyncio.Task | None = None
    if a2a_port is not None:
        try:
            import uvicorn
        except Exception as exc:  # stx-allow: fallback (reason: optional dep; runner stays alive heart-beating even if server can't bind)
            # Broad: uvicorn import can fail with non-ImportError when
            # transitive deps (httptools, websockets) are mis-built.
            logger.error("a2a_proxy needs uvicorn: %s", exc)
        else:
            app = build_app(
                name=name,
                upstream=upstream,
                trust=trust,
                redact=redact,
                timeout_s=timeout_s,
                upstream_card=upstream_card,
                upstream_card_error=fetch_err,
            )
            config = uvicorn.Config(
                app,
                host=a2a_host,
                port=a2a_port,
                log_level="warning",
                ws="none",
                lifespan="off",
            )
            server = uvicorn.Server(config)
            # Suppress uvicorn's own signal handlers: it installs
            # signal.signal(SIGTERM/SIGINT) via ``Server.install_signal_handlers``,
            # which (a) shadows the asyncio-level handlers we
            # registered above and (b) is never restored on shutdown.
            # The latter leaks process-global signal dispositions
            # into whatever runs after ``run()`` returns — observed:
            # subsequent Starlette TestClient SSE streams produce
            # empty bodies because httpx's stream reader is
            # interrupted by the dangling handler.
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            serve_task = asyncio.create_task(server.serve())

    try:
        await stop.wait()
    finally:
        if serve_task is not None and not serve_task.done():
            serve_task.cancel()
            try:
                await serve_task
            except (
                asyncio.CancelledError,
                Exception,
            ):  # stx-allow: fallback (reason: defensive cleanup)
                pass
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass
        # Restore the signal handlers we installed so this runner
        # leaves the process in the same state it found it. Critical
        # for in-process callers (tests, supervisor harnesses) — see
        # the asyncio_handlers/fallback_handlers comment above.
        for sig in asyncio_handlers:
            try:
                loop.remove_signal_handler(sig)
            except (
                NotImplementedError,
                ValueError,
            ):  # stx-allow: fallback (reason: handler already gone)
                pass
        for sig, prev in fallback_handlers.items():
            if prev is not None:
                signal.signal(sig, prev)
        write_heartbeat(state_dir, pid=pid, state=STATE_STOPPING)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_argv(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scitex_agent_container._runners.a2a_proxy",
        description="AgentProxy runner — forward /v1/turn to an upstream A2A.",
    )
    p.add_argument("--name", required=True)
    p.add_argument("--state-root", type=Path, default=None)
    p.add_argument("--upstream", required=True)
    p.add_argument("--trust", default="untrusted")
    p.add_argument(
        "--redact",
        default="",
        help="Comma-separated substring tokens; matched prompts are refused (400).",
    )
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--tick-seconds", type=float, default=DEFAULT_TICK_SECONDS)
    p.add_argument("--a2a-port", type=int, default=None)
    p.add_argument("--a2a-host", default="127.0.0.1")
    p.add_argument("--a2a-card-yaml", default="")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_argv(argv)
    redact = [t.strip() for t in (args.redact or "").split(",") if t.strip()]
    return asyncio.run(
        run(
            args.name,
            upstream=args.upstream,
            trust=args.trust,
            redact=redact,
            timeout_s=args.timeout_s,
            state_root=args.state_root,
            tick_seconds=args.tick_seconds,
            a2a_host=args.a2a_host,
            a2a_port=args.a2a_port,
            a2a_card_yaml=args.a2a_card_yaml,
        )
    )


if __name__ == "__main__":  # pragma: no cover — exercised by adapter
    sys.exit(main())


_ = DEFAULT_STATE_ROOT  # keep import for compat with claude_session symmetry
