"""Lazy SciTeX logging access for SAC diagnostics and output transports.

Human diagnostics use get_logger(); rendered results use render_rich(). Exact
protocol frames use render_content() or write_stream() with a dedicated
message-only formatter. All output goes through scitex-logging.
"""

from __future__ import annotations

from typing import Any

from ._streams import render_content, render_rich, write_stream

__all__ = ["get_logger", "render_content", "render_rich", "write_stream"]


def get_logger(name: str) -> Any:
    """Return the ``scitex-logging`` logger for ``name``.

    ``name`` should be the caller's ``__name__`` so the emitted record carries
    the module the diagnostic came from — that origin is the whole point.

    Import lazily so command discovery does not configure process logging.
    """
    import scitex_logging

    return scitex_logging.getLogger(name)
