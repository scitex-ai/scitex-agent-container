#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SciTeX logging adapters for human output and caller-owned protocol streams.

Human output carries its logging level. Protocol transports use a dedicated
logger and a message-only formatter to preserve JSON, shell completion, and
caller-supplied stream contracts. No writer uses raw print().
"""

from __future__ import annotations

import logging
import re
from threading import RLock
from typing import TextIO

__all__ = ["render_content", "render_rich", "write_stream"]

_STREAM_LOCK = RLock()
_CONTENT_LOGGER: logging.Logger | None = None
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


class _ProtocolHandler(logging.StreamHandler):
    """Keep transport failures visible to the caller that owns the stream."""

    def __init__(self, stream: TextIO | None = None, *, flush: bool = True) -> None:
        self._flush_output = flush
        super().__init__(stream)

    def flush(self) -> None:
        if self._flush_output:
            super().flush()

    def handleError(self, record: logging.LogRecord) -> None:
        # StreamHandler normally reports errors to stderr and then returns.
        # A lost JSON frame must fail its caller instead of looking successful.
        raise


class _ProtocolConsoleHandler(_ProtocolHandler):
    """Retain SciTeX's current-stdout resolution while exposing I/O errors."""

    def __init__(self, console_handler: logging.StreamHandler) -> None:
        self._console_handler = console_handler
        super().__init__()

    @property
    def stream(self) -> TextIO:
        return self._console_handler.stream

    @stream.setter
    def stream(self, value: TextIO) -> None:
        # The SciTeX handler resolves stdout dynamically, including its
        # print-capture bypass, so a redirect never leaves a stale destination.
        pass


def render_content(content: str) -> None:
    """Emit protocol content through a dedicated SciTeX stdout logger."""
    import scitex_logging

    global _CONTENT_LOGGER
    with _STREAM_LOCK:
        if _CONTENT_LOGGER is None:
            console = scitex_logging.getConsole(f"{__name__}.content")
            for original in console.handlers[:]:
                handler = _ProtocolConsoleHandler(original)
                handler.setFormatter(logging.Formatter("%(message)s"))
                console.removeHandler(original)
                console.addHandler(handler)
                original.close()
            _CONTENT_LOGGER = console
        _CONTENT_LOGGER.info(content)


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

    # Dedicated names keep a transport's handler from changing the destination
    # of the application's diagnostic logger. Serialize temporary handlers so
    # concurrent callers cannot emit into one another's streams.
    with _STREAM_LOCK:
        logger = scitex_logging.getLogger(f"{__name__}.stream")
        logger.setLevel(scitex_logging.INFO)
        logger.propagate = False
        handler = _ProtocolHandler(stream, flush=flush)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        try:
            logger.info(text)
        finally:
            logger.removeHandler(handler)
