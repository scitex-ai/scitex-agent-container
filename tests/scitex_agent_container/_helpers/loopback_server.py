"""Wait for a REAL uvicorn loopback server to come up — generously, and loudly.

Why this exists (CI failure 2026-07-13, develop ``b92f38c3``, py3.11 leg:
``1 failed, 9890 passed`` — ``test_cross_host_send_with_explicit_grant_...``):
six test modules had each hand-rolled the same startup wait —

    deadline = time.monotonic() + 5.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn loopback did not start in 5s")
        time.sleep(0.05)

— and that 5-second ceiling is a RACE BY CONSTRUCTION. uvicorn sets
``server.started`` only AFTER the ASGI **lifespan startup** completes, and the
listen app's lifespan awaits ``persist_self_peers_on_listen_startup()`` (a
filesystem walk over the config/fleet search dirs plus SQLite upserts) before
spawning its six background loops. Measured on a loaded box: the server came
up in **7.49s** — against the 5.0s ceiling. On a 2-core CI runner under load
that is a coin flip, and it is why the leg went red without hanging.

Two things are fixed here, once, for every call site:

* **Poll, with a GENEROUS ceiling.** The wait already polled the real ready
  state, so the fast path returns the instant the server binds — a generous
  ceiling therefore costs *nothing* when the server is healthy and only bounds
  the pathological case. Raising a tight deadline is not "moving the flake"
  when the thing you are waiting on is a polled predicate rather than a fixed
  sleep. Same reasoning as ``_settle`` in ``test__tui_heartbeat_loop.py``:
  "poll for the expected end-state instead of guessing a duration".

* **Fail loud, and say WHICH failure it was.** The old wait could not tell a
  SLOW server from a DEAD one. If ``server.run()`` raised (port collision, bad
  config), the thread exited, ``started`` stayed False, the wait burned its
  whole ceiling and then blamed a *timeout* — while the real exception was
  swallowed and never printed. Here the server thread's exception is captured
  and re-raised as the ``__cause__``, so a genuine startup failure names itself
  instead of masquerading as slowness.

NO MOCKS — a real ``uvicorn.Server`` on a real loopback port.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from typing import Any, Iterator

import uvicorn

# Generous on purpose: the happy path returns as soon as the port is serving,
# so this only ever bounds a genuinely broken server. Overridable for a very
# slow runner via SAC_TEST_LOOPBACK_STARTUP_TIMEOUT_S.
DEFAULT_STARTUP_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.02
_SHUTDOWN_JOIN_TIMEOUT_S = 10.0

__all__ = [
    "DEFAULT_STARTUP_TIMEOUT_S",
    "await_until_serving",
    "run_loopback",
    "serve_in_thread",
    "wait_until_serving",
]


def _startup_timeout_s(explicit: float | None = None) -> float:
    if explicit is not None:
        return explicit
    raw = os.environ.get("SAC_TEST_LOOPBACK_STARTUP_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_STARTUP_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_STARTUP_TIMEOUT_S


def serve_in_thread(server: uvicorn.Server, port: int) -> tuple[threading.Thread, list]:
    """Run ``server`` in a daemon thread; return ``(thread, crash_box)``.

    ``crash_box`` receives the exception if ``server.run()`` raises, so the
    waiter can re-raise it on the test thread instead of losing it.
    """
    crash: list[BaseException] = []

    def _serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # stx-allow: fallback (reason: relayed to the test thread by wait_until_serving; swallowing it here is exactly the old bug)
            crash.append(exc)

    thread = threading.Thread(
        target=_serve, name=f"uvicorn-loopback-{port}", daemon=True
    )
    thread.start()
    return thread, crash


def _check_dead(
    server: uvicorn.Server,
    thread: threading.Thread,
    crash: list,
    port: int,
    elapsed: float,
) -> None:
    """Raise (naming the real cause) if the server thread died before serving."""
    if crash:
        raise RuntimeError(
            f"uvicorn loopback on port {port} CRASHED during startup after "
            f"{elapsed:.2f}s — see the chained exception for the real cause"
        ) from crash[0]
    if not thread.is_alive() and not server.started:
        raise RuntimeError(
            f"uvicorn loopback on port {port} exited during startup after "
            f"{elapsed:.2f}s without ever serving, and raised nothing"
        )


def _timed_out(port: int, elapsed: float, ceiling: float, alive: bool) -> RuntimeError:
    return RuntimeError(
        f"uvicorn loopback on port {port} never became ready: server.started "
        f"stayed False for {elapsed:.1f}s (ceiling {ceiling:.1f}s; server thread "
        f"alive={alive}). uvicorn only flips `started` once the ASGI LIFESPAN "
        f"startup has completed, so the lifespan is what never finished."
    )


def wait_until_serving(
    server: uvicorn.Server,
    thread: threading.Thread,
    *,
    port: int,
    crash: list | None = None,
    timeout_s: float | None = None,
) -> None:
    """Block until ``server`` is serving. Fail loud on death or timeout."""
    ceiling = _startup_timeout_s(timeout_s)
    started_at = time.monotonic()
    crash = crash if crash is not None else []
    while not server.started:
        elapsed = time.monotonic() - started_at
        _check_dead(server, thread, crash, port, elapsed)
        if elapsed > ceiling:
            raise _timed_out(port, elapsed, ceiling, thread.is_alive())
        time.sleep(_POLL_INTERVAL_S)


async def await_until_serving(
    server: uvicorn.Server,
    thread: threading.Thread,
    *,
    port: int,
    crash: list | None = None,
    timeout_s: float | None = None,
) -> None:
    """``wait_until_serving`` for an async test — yields to the loop while waiting."""
    ceiling = _startup_timeout_s(timeout_s)
    started_at = time.monotonic()
    crash = crash if crash is not None else []
    while not server.started:
        elapsed = time.monotonic() - started_at
        _check_dead(server, thread, crash, port, elapsed)
        if elapsed > ceiling:
            raise _timed_out(port, elapsed, ceiling, thread.is_alive())
        await asyncio.sleep(_POLL_INTERVAL_S)


@contextlib.contextmanager
def run_loopback(
    app: Any, port: int, *, timeout_s: float | None = None, **config_kwargs: Any
) -> Iterator[int]:
    """Serve ``app`` on ``127.0.0.1:port`` for the duration of the block.

    Teardown flips ``should_exit`` and joins, so the ``finally`` fires even on
    test failure and a hung client never strands the server.
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws="none",
        **config_kwargs,
    )
    server = uvicorn.Server(config)
    thread, crash = serve_in_thread(server, port)
    wait_until_serving(
        server, thread, port=port, crash=crash, timeout_s=timeout_s
    )
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_S)
