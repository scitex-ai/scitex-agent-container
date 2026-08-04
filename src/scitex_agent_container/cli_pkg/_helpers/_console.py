"""Shared rich Console + sac-system status-line printer.

System messages route through ``scitex-logging`` so the ecosystem's
log-format env vars (``SCITEX_LOGGING_LEVEL``, ``SCITEX_LOGGING_FORMAT``)
control sac's lifecycle prose alongside every other scitex package.
The rich ``Console`` is kept around for tabular renders
(``sac agents list``) that benefit from rich-text layout.
"""

from __future__ import annotations

import scitex_logging
from rich.console import Console

console = Console()
logger = scitex_logging.getLogger("scitex_agent_container")


def system_msg(text: str, style: str = "info") -> None:
    """Emit a sac-system lifecycle line via the project logger.

    The ``=== ... ===`` framing marks the sac-system boundary so it
    doesn't get confused with the agent's own stdout (see operator
    note about new users mistaking sac prose for Claude's reply).

    ``style`` maps to a scitex-logging level:

      * ``"info"``  (default) — lifecycle progress (starting / stopping)
      * ``"dim"``             — ancillary notices (preflight skipped,
                                session override) — currently
                                downgraded to DEBUG so they only show
                                under ``SCITEX_LOGGING_LEVEL=DEBUG``
      * ``"green"`` / ``"success"``  — completion summary (started, deleted)
      * ``"red"``   / ``"error"``    — unambiguous failure (renders ``ERRO``)
      * ``"fail"``                   — a CHECK that failed (renders ``FAIL``)
      * ``"yellow"``/ ``"warn"``     — warnings (overwrite confirmations etc.)

    Rich markup tags inside ``text`` get stripped before logging since
    scitex-logging applies its own per-level ANSI colours.
    """
    msg = _strip_rich_markup(text)
    level = _STYLE_TO_LEVEL.get(style, scitex_logging.INFO)
    logger.log(level, msg)


_STYLE_TO_LEVEL = {
    "info": scitex_logging.INFO,
    "blue": scitex_logging.INFO,
    "dim": scitex_logging.DEBUG,
    "green": scitex_logging.SUCCESS,
    "success": scitex_logging.SUCCESS,
    # ERRO is the doctrine's name for an unambiguous failure; FAIL stays
    # available for a caller reporting a failed CHECK rather than an error.
    "red": scitex_logging.ERROR,
    "error": scitex_logging.ERROR,
    "fail": scitex_logging.FAIL,
    "yellow": scitex_logging.WARNING,
    "warn": scitex_logging.WARNING,
    "warning": scitex_logging.WARNING,
}


def _strip_rich_markup(text: str) -> str:
    """Remove ``[tag]`` / ``[/tag]`` rich markup pairs so the logger
    formatter doesn't show them as literal brackets in its output.

    Best-effort; arbitrary nesting works (the regex matches each tag
    in isolation). Doesn't touch text without brackets.
    """
    import re

    return re.sub(r"\[/?[a-zA-Z][a-zA-Z0-9_ ]*\]", "", text)
