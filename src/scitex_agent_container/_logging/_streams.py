#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Caller-owned stream writes — the PS-220 content-transport primitive.

PS-220 forbids a bare ``print`` in shippable SciTeX source because a caller
importing the module cannot silence, redirect or capture it: there is no flag,
no handler and no level. The rule spares exactly three *mechanically provable*
transports, and one of them is a **caller-owned required stream** — a ``print``
whose ``file=`` is a REQUIRED parameter of the enclosing function, so the
CALLER, not this module, owns the destination.

``write_stream`` is that transport, factored out so the call sites that must
honour a caller-supplied stream (a captured ``io.StringIO`` in tests, a
redirected CLI stream, a cron job's own log sink) do not each re-derive it.
The stream is a REQUIRED parameter: this module never chooses a destination, so
nothing here is "library code writing unconditionally to stdout".

This is deliberately NOT a general ``print`` escape hatch. Routing a
caller-directed payload through a logger would be wrong, not merely noisy:
scitex-logging writes every console record to STDERR, which would corrupt a
machine-readable payload the caller asked to receive on its own stream.
"""

from __future__ import annotations

from typing import TextIO

__all__ = ["render_content", "render_rich", "write_stream"]


def render_content(content: str) -> None:
    """Print caller-supplied, already-rendered content verbatim to stdout.

    This is the *explicit content-rendering contract* PS-220 recognises
    structurally: the enclosing API is an output operation that emits its
    caller-supplied content unchanged. It exists for the handful of product
    outputs whose exact bytes are a published contract — ``sac-statusline``
    (consumed by Claude Code as the pane contents), a shell completion
    script that gets ``source``d — where a logging level prefix or a hop to
    stderr would corrupt the payload rather than clarify it.

    It is NOT a general ``print`` hatch: human-facing status and diagnostics
    belong on ``scitex_logging.getConsole``/``getLogger``, which carry the
    level, the aligned prefix and the searchable record the mandate exists
    for.
    """
    print(content)


def render_rich(renderable, name: str, *, level: str = "info") -> None:
    """Render a Rich renderable through the SciTeX stdout console.

    Rich's ``Console.print`` is forbidden in shippable source (PS-220) and has
    no spare path: it always writes to a console stream that carries no level,
    no aligned prefix and no searchable record. So the renderable (a ``Table``,
    a markup string) is rendered with Rich's own renderer to text — the console
    stream is never written to — and that text is emitted as ONE levelled
    record. The table a reader sees is byte-identical; the operator gains the
    level.

    Parameters
    ----------
    renderable : Any
        Any Rich renderable: ``rich.table.Table``, markup ``str``, …
    name : str
        Logger name — pass ``__name__`` from the call site.
    level : str
        One of ``info``/``warning``/``error``/``success``.
    """
    import scitex_logging as slogging
    from rich.console import Console

    console = Console()
    lines = console.render_lines(renderable, console.options, pad=False)
    text = "\n".join("".join(segment.text for segment in line) for line in lines)
    getattr(slogging.getConsole(name), level)(text.rstrip("\n"))


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
    print(text, file=stream, flush=flush)
