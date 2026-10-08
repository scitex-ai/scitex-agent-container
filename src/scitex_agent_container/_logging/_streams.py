#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SciTeX logging adapters for human output and caller-owned protocol streams.

Human output carries its logging level. Protocol transports use SciTeX's plain
writer to preserve JSON, shell completion, and caller-supplied stream contracts
independently of diagnostic thresholds. No writer uses raw print().
"""

from __future__ import annotations

import logging
import re
from threading import RLock
from typing import TextIO

__all__ = ["render_content", "render_rich", "write_stream"]

_STREAM_LOCK = RLock()
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


class _RedirectAwareFormatter(logging.Formatter):
    """Keep SciTeX's human format while removing colors from redirected output."""

    def __init__(
        self, formatter: logging.Formatter, handler: logging.StreamHandler
    ) -> None:
        super().__init__()
        self._formatter = formatter
        self._handler = handler

    def format(self, record: logging.LogRecord) -> str:
        formatted = self._formatter.format(record)
        try:
            is_terminal = self._handler.stream.isatty()
        except (AttributeError, OSError, ValueError):
            is_terminal = False
        # Older SciTeX formatters honor a process-wide force-color flag even
        # when redirected. Adapt only this SAC console's destination; retain
        # the configured prefix, continuation lines, and real-terminal color.
        return formatted if is_terminal else _ANSI_SGR.sub("", formatted)


def render_content(content: str) -> None:
    """Emit protocol content through SciTeX's unfiltered stdout writer."""
    import scitex_logging
    import sys

    with _STREAM_LOCK:
        try:
            plain = scitex_logging.getPlainConsole(__name__)
        except AttributeError:
            # scitex-logging<0.2.1 has no plain console; mirror the
            # released contract (own stdout, trailing newline) directly.
            sys.stdout.write(content + "\n")
        else:
            plain.emit(content)


def render_rich(
    renderable, name: str, *, level: str = "info", width: int | None = None
) -> None:
    """Render a Rich renderable through the SciTeX stdout console.

    Rich's ``Console.print`` is forbidden in shippable source (PS-220) and has
    no spare path: it always writes to a console stream that carries no level,
    no aligned prefix and no searchable record. So the renderable (a ``Table``,
    a markup string) is rendered with Rich's own renderer to text — the console
    stream is never written to — and that text is emitted as ONE levelled
    record. The table layout is preserved and the output carries its level.
    SAC keeps redirected output free of color escapes even when SciTeX's
    process-wide force-color setting is enabled. A terminal destination keeps
    SciTeX's configured colors; other SciTeX consoles are unaffected.

    Parameters
    ----------
    renderable : Any
        Any Rich renderable: ``rich.table.Table``, markup ``str``, …
    name : str
        Logger name — pass ``__name__`` from the call site.
    level : str
        One of ``info``/``warning``/``error``/``success``.
    width : int | None
        Render width. ``None`` (default) keeps the previous behaviour —
        a fresh default-width console. Pass the caller's console width
        when the table must honour it (tabular listings whose tests pin
        a wide console so long values stay contiguous); without it a
        wide pin on the caller's console is silently ignored and the
        table wraps at 80.
    """
    import scitex_logging as slogging
    from rich.console import Console

    console = Console(width=width) if width else Console()
    lines = console.render_lines(renderable, console.options, pad=False)
    text = "\n".join("".join(segment.text for segment in line) for line in lines)
    with _STREAM_LOCK:
        logger = slogging.getConsole(f"{name}.console", level=slogging.get_level())
        for handler in logger.handlers:
            formatter = handler.formatter
            if formatter is not None and not isinstance(
                formatter, _RedirectAwareFormatter
            ):
                handler.setFormatter(_RedirectAwareFormatter(formatter, handler))
        getattr(logger, level)(text.rstrip("\n"))


def write_stream(text: str, stream: TextIO, *, flush: bool = False) -> None:
    """Write one already-rendered line verbatim to a caller-owned stream.

    Parameters
    ----------
    text : str
        The already-rendered line. Passed through unchanged — no level prefix
        is added, because the caller owns both the payload and the stream.
    stream : TextIO
        The caller-supplied destination. Required, so the destination is never
        chosen here.
    flush : bool
        Flush after writing. Used by the one-shot startup diagnostics that
        must reach the operator before a slow import continues.

    Returns
    -------
    None
    """
    import scitex_logging

    # Keep transport frames serialized without changing diagnostic loggers or
    # taking ownership of the caller's stream.
    with _STREAM_LOCK:
        try:
            plain = scitex_logging.getPlainConsole(__name__)
        except AttributeError:
            # scitex-logging<0.2.1 has no plain console; mirror the
            # released contract onto the caller's stream directly.
            stream.write(text + "\n")
            if flush:
                stream.flush()
            return
        try:
            # scitex-logging>=0.2.3 caller-stream support; older releases
            # only accept the message and always target their own stdout.
            plain.emit(text, stream=stream, flush=flush)
        except TypeError:
            # Released emit() targets its own stdout with a trailing
            # newline; mirror that contract onto the caller's stream.
            stream.write(text + "\n")
            if flush:
                stream.flush()
