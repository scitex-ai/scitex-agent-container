#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Heartbeat progress for long ``sac agents start`` runs.

A start can sit silent for MINUTES — the boot drain alone waits up to
240 s for ``startup_commands``-delayed bootstraps, and image
provisioning / credential preflights / hooks add more — while the only
line on screen is ``starting <name>``. An operator staring at that
cannot tell a working start from a wedged one (operator report
2026-10-19: "sac-start takes minutes with no progress output").

:func:`starting_progress` wraps the blocking launch call and emits a
``still starting '<name>' — <elapsed>s elapsed`` line every
``interval_s`` until the launch returns. Info-level (progress chatter,
not a fault), daemon-threaded so a wedged launch can never be kept
alive by its own heartbeat, and JSON-safe by construction — the caller
enables it only for human console output, never for ``--json`` where
stdout must stay a pure envelope.

Test seam: :data:`HEARTBEAT_INTERVAL_S` is module-level so tests drive
a fast tick without touching the production 30 s cadence.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .._helpers import system_msg

__all__ = ["HEARTBEAT_INTERVAL_S", "starting_progress"]

#: Seconds between "still starting" heartbeat lines in production.
HEARTBEAT_INTERVAL_S = 30.0


@contextmanager
def starting_progress(
    name: str, *, enabled: bool = True, interval_s: float = HEARTBEAT_INTERVAL_S
) -> Iterator[None]:
    """Emit periodic "still starting" lines until the launch returns.

    Args:
        name: Agent name shown in the heartbeat line.
        enabled: ``False`` disables all output (``--json`` mode and
            callers that own their own progress surface).
        interval_s: Seconds between heartbeat lines.
    """
    if not enabled:
        yield
        return
    stop = threading.Event()

    def _beat() -> None:
        began = time.monotonic()
        while not stop.wait(interval_s):
            elapsed = int(time.monotonic() - began)
            system_msg(
                f"still starting '{name}' — {elapsed}s elapsed "
                f"(boot drain / image / hooks can take minutes)",
                style="info",
            )

    worker = threading.Thread(target=_beat, name=f"sac-start-progress-{name}", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
