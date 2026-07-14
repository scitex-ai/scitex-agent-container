"""SIGTERM/SIGINT guard for ``sac listen``'s bounded startup decision.

``resolve_startup`` may spend a few seconds re-checking a holder that
failed its health check. A ``systemctl stop`` (or a Ctrl-C) landing in
that window must produce a PROMPT, CLEAN exit — not a ``KeyboardInterrupt``
traceback, and not a process that ignores the signal until its next poll.

The handler does the minimum a signal handler may safely do: flip a flag.
The decision loop polls that flag on ~250ms boundaries, so the wait never
blocks the signal. Prior handlers are restored on exit, so uvicorn (which
only starts once the loop owns the lock) installs its own graceful-shutdown
handlers unchanged.
"""

from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import Iterator

__all__ = ["StopFlag", "stop_flag_guard"]


class StopFlag:
    """A one-way stop flag flipped by a signal handler, polled by the loop."""

    __slots__ = ("_tripped",)

    def __init__(self) -> None:
        self._tripped = False

    def trip(self, *_args: object) -> None:
        """Signal-handler entrypoint — minimal work: flip the flag."""
        self._tripped = True

    def is_set(self) -> bool:
        return self._tripped


@contextmanager
def stop_flag_guard() -> Iterator[StopFlag]:
    """Install SIGTERM/SIGINT handlers that trip a :class:`StopFlag`.

    Yields the flag. Restores the previous handlers on exit.

    Handlers can only be installed from the main thread; the ``sac
    listen`` CLI runs there. Off the main thread this degrades to a no-op
    guard (yielding a flag nothing will ever trip) rather than crashing
    the boot.
    """
    flag = StopFlag()
    try:
        prev_term = signal.signal(signal.SIGTERM, flag.trip)
        prev_int = signal.signal(signal.SIGINT, flag.trip)
    except ValueError:
        # Not the main thread — cannot install signal handlers.
        yield flag
        return
    try:
        yield flag
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
