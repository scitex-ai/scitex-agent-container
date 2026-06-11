"""A2A → tmux turn bridge.

The pre-SDK tmux runner had no inbound-turn mechanism. Day-2 (B) of
the ``tui-driver-runtime`` revival adds one that mirrors the SDK
runner's ``/v1/turn`` surface so an A2A caller cannot tell the two
runtimes apart on the wire.

Surface shape (mirrors ``_runners/_session_http.serve_inbound``):

    POST /v1/turn
    Content-Type: application/json
    {"text": "your message", "exit_after": false}

    200 OK
    {"text": "<pane delta>", "session_id": null, "exit_after": false,
     "metadata": {"timeout_s": <N>}}

    504 Gateway Timeout
    {"status": "timeout_wait_elapsed",
     "detail": "<honest explanation; turn may still be running>",
     "timeout_s": <N>, "error": "turn exceeded <N>s timeout"}

Mechanics:
1. ``send_keys(text)`` then ``send_keys("Enter")`` into the tmux session
   (separate calls — the tmux ``Enter`` keyword is more reliable than a
   trailing ``\\r`` per the salvaged ``tmux.py`` lessons).
2. Poll ``capture_pane`` every ``poll_interval`` seconds for the
   ready marker (``is_ready`` from the salvaged ``prompts.py``).
3. Hard-cap at ``SAC_TMUX_TURN_TIMEOUT_S`` (default 300 s = 5 min).
   On timeout return 504 with a structured body AND leave the tmux
   session ALIVE — the next turn may still work.

Design seam: ``TmuxDriver`` is a ``typing.Protocol`` so tests can
inject a memory-backed fake. The default implementation calls
``subprocess.run``-style helpers; tests never touch real tmux.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Protocol

from .prompts import is_ready

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_TURN_TIMEOUT_S: float = 300.0  # 5 min (B.4)
DEFAULT_POLL_INTERVAL_S: float = 1.0
TURN_TIMEOUT_ENV_VAR: str = "SAC_TMUX_TURN_TIMEOUT_S"
POLL_INTERVAL_ENV_VAR: str = "SAC_TMUX_TURN_POLL_S"


def _resolve_turn_timeout(explicit: float | None) -> float:
    """Pick the effective turn timeout (explicit > env > default).

    Loud on a malformed env value — STX hard-rule "no silent fallbacks".
    """
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(TURN_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_TURN_TIMEOUT_S
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{TURN_TIMEOUT_ENV_VAR}={raw!r} is not a valid float seconds"
        ) from exc


def _resolve_poll_interval(explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(POLL_INTERVAL_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_POLL_INTERVAL_S
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{POLL_INTERVAL_ENV_VAR}={raw!r} is not a valid float seconds"
        ) from exc


# ---------------------------------------------------------------------------
# Driver protocol + result type
# ---------------------------------------------------------------------------


class TmuxDriver(Protocol):
    """Real-tmux abstraction so tests don't need ``tmux`` installed.

    Implemented for real by :class:`SubprocessTmuxDriver` (uses the
    salvaged ``tmux.py::TmuxManager.send_keys`` / ``capture_content``);
    fakes used by the test suite hold an in-memory pane buffer.
    """

    def send_keys(self, session: str, *keys: str) -> None: ...
    def capture_pane(self, session: str) -> str: ...
    def session_exists(self, session: str) -> bool: ...


@dataclass(frozen=True)
class TurnResult:
    """The structured outcome of one A2A→tmux turn.

    ``text`` is the pane *delta* — the lines that arrived AFTER the
    turn was injected. ``timed_out`` is True iff the ready marker was
    not detected within the per-turn budget; the tmux session is
    NEVER killed in that case (per B.4).
    """

    text: str
    timed_out: bool
    elapsed_s: float
    poll_count: int


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class TurnTimeoutError(Exception):
    """Raised when the ready marker is not seen within the turn budget.

    Carries the structured ``TurnResult`` (with ``timed_out=True``)
    on ``self.result`` so the HTTP layer can build a 504 body without
    a second poll.
    """

    def __init__(self, result: TurnResult, session: str):
        self.result = result
        self.session = session
        super().__init__(
            f"tmux turn on session {session!r} exceeded "
            f"{result.elapsed_s:.1f}s without a ready marker"
        )


def inject_turn(
    driver: TmuxDriver,
    session: str,
    turn_text: str,
    *,
    timeout_s: float | None = None,
    poll_interval_s: float | None = None,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> TurnResult:
    """Send ``turn_text`` into ``session`` and wait for the ready marker.

    Returns a :class:`TurnResult` carrying the pane delta and timing
    metadata. Raises :class:`TurnTimeoutError` (carrying the same
    ``TurnResult``) when the ready marker does not appear within the
    bounded budget. The tmux session is NEVER killed on timeout.

    Sequence (per B.1 / B.2 / B.3):
      1. Capture the pre-turn pane content (baseline).
      2. ``send_keys(turn_text)`` then ``send_keys("Enter")``.
      3. Poll ``capture_pane`` until ``is_ready(content)`` AND content
         differs from the baseline (so we don't return a stale baseline
         that already looked "ready").
      4. Return the suffix of the new content after the baseline.
    """
    timeout = _resolve_turn_timeout(timeout_s)
    poll = _resolve_poll_interval(poll_interval_s)

    baseline = driver.capture_pane(session)
    driver.send_keys(session, turn_text)
    driver.send_keys(session, "Enter")

    start = monotonic_fn()
    poll_count = 0
    last_content = baseline
    while True:
        poll_count += 1
        last_content = driver.capture_pane(session)
        elapsed = monotonic_fn() - start
        if is_ready(last_content) and last_content != baseline:
            return TurnResult(
                text=_pane_delta(baseline, last_content),
                timed_out=False,
                elapsed_s=elapsed,
                poll_count=poll_count,
            )
        if elapsed >= timeout:
            result = TurnResult(
                text=_pane_delta(baseline, last_content),
                timed_out=True,
                elapsed_s=elapsed,
                poll_count=poll_count,
            )
            logger.warning(
                "tmux turn timed out after %.1fs on session %s "
                "(polls=%d) — session preserved",
                elapsed,
                session,
                poll_count,
            )
            raise TurnTimeoutError(result, session)
        sleep_fn(poll)


def _pane_delta(baseline: str, current: str) -> str:
    """Return the suffix of ``current`` that arrived AFTER ``baseline``.

    Best-effort string-prefix diff: when ``current`` starts with
    ``baseline``, returns the tail; otherwise returns ``current``
    verbatim (the pane may have scrolled out of view). The bridge
    never tries to be clever here — clever delta logic is the
    consumer's problem.
    """
    if current.startswith(baseline):
        return current[len(baseline) :]
    return current


# ---------------------------------------------------------------------------
# Default driver (real tmux)
# ---------------------------------------------------------------------------


class SubprocessTmuxDriver:
    """Default :class:`TmuxDriver` — wraps the salvaged ``tmux.py``.

    Lives here (not in ``tmux.py``) so the bridge module can be
    imported and exercised in tests that never touch real tmux.
    """

    def send_keys(self, session: str, *keys: str) -> None:
        from .tmux import TmuxManager

        TmuxManager.send_keys(session, *keys)

    def capture_pane(self, session: str) -> str:
        from .tmux import TmuxManager

        return TmuxManager.capture_content(session)

    def session_exists(self, session: str) -> bool:
        from .tmux import TmuxManager

        return TmuxManager.exists(session)


# ---------------------------------------------------------------------------
# HTTP server (mirrors _session_http.serve_inbound shape)
# ---------------------------------------------------------------------------


async def serve_tmux_inbound(
    *,
    host: str,
    port: int,
    session: str,
    driver: TmuxDriver | None = None,
    turn_timeout_s: float | None = None,
    poll_interval_s: float | None = None,
    stop=None,
) -> None:
    """Run an HTTP server that bridges ``/v1/turn`` into a tmux session.

    Wire format and response codes mirror
    :func:`._session_http.serve_inbound` so an A2A caller cannot tell
    the SDK runner and the tmux runner apart.

    The handler is intentionally lightweight: each POST is one
    synchronous-ish drive of :func:`inject_turn` on the configured
    session, awaited in a thread so we don't block the event loop.
    Concurrent POSTs are serialised by an internal lock so two callers
    can't trample one tmux pane.
    """
    import asyncio

    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route
    except ImportError as exc:  # stx-allow: fallback (optional dep)
        logger.error("tmux inbound HTTP requires starlette+uvicorn: %s", exc)
        return

    effective_timeout = _resolve_turn_timeout(turn_timeout_s)
    effective_poll = _resolve_poll_interval(poll_interval_s)
    drv = driver or SubprocessTmuxDriver()
    lock = asyncio.Lock()
    stop_event = stop or asyncio.Event()

    async def post_turn(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError as exc:  # stx-allow: malformed JSON → 400
            return JSONResponse({"error": f"bad JSON: {exc}"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "missing or empty 'text' field"}, status_code=400
            )
        exit_after = bool(body.get("exit_after", False))

        async with lock:
            try:
                result = await asyncio.to_thread(
                    inject_turn,
                    drv,
                    session,
                    text,
                    timeout_s=effective_timeout,
                    poll_interval_s=effective_poll,
                )
            except TurnTimeoutError as exc:
                # 504: ready marker not observed within budget; the
                # tmux session is still alive (B.4) and the next turn
                # may still work. Mirror _session_http's 504 schema.
                return JSONResponse(
                    {
                        "status": "timeout_wait_elapsed",
                        "detail": (
                            f"The bounded wait of {effective_timeout:.0f}s "
                            "elapsed without a ready marker. The tmux "
                            "session is preserved; the turn may still "
                            "be running. A timeout does not necessarily "
                            "mean failure."
                        ),
                        "timeout_s": effective_timeout,
                        "session_id": None,
                        "heartbeat": None,
                        "text": exc.result.text,
                        "error": (f"turn exceeded {effective_timeout:.0f}s timeout"),
                    },
                    status_code=504,
                )

        return JSONResponse(
            {
                "text": result.text,
                "session_id": None,
                "exit_after": exit_after,
                "metadata": {
                    "timeout_s": effective_timeout,
                    "elapsed_s": result.elapsed_s,
                    "poll_count": result.poll_count,
                },
            }
        )

    async def get_health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "runtime": "tmux", "session": session})

    routes = [
        Route("/v1/turn", post_turn, methods=["POST"]),
        Route("/health", get_health, methods=["GET"]),
    ]
    app = Starlette(routes=routes)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        ws="none",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            serve_task.cancel()
            try:
                await serve_task
            except (
                asyncio.CancelledError,
                Exception,
            ):  # stx-allow: defensive cleanup
                pass


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_TURN_TIMEOUT_S",
    "POLL_INTERVAL_ENV_VAR",
    "SubprocessTmuxDriver",
    "TURN_TIMEOUT_ENV_VAR",
    "TmuxDriver",
    "TurnResult",
    "TurnTimeoutError",
    "inject_turn",
    "serve_tmux_inbound",
]
