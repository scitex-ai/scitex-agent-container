"""Shared helpers for sac MCP tool registration.

The vast majority of sac CLI commands already expose ``--json`` for
machine-readable output. Each MCP tool wraps a CLI command by invoking
it through Click's ``CliRunner`` and decoding the JSON. Commands that
don't yet have ``--json`` either get a thin Python-API wrapper or are
exposed as best-effort plain-text returns.

This shim keeps the parity invariant trivially auditable: a missing
MCP tool means a missing CLI ``--json`` flag (or vice-versa).
"""

from __future__ import annotations

import json
from typing import Any

from click.testing import CliRunner


def invoke_cli_json(argv: list[str]) -> dict[str, Any]:
    """Invoke the top-level ``sac`` Click app with ``argv`` and parse
    its STDOUT as JSON.

    Returns ``{"exit_code", "data", "stdout", "stderr"}``. ``data`` is the
    parsed JSON when stdout decoded cleanly; ``None`` otherwise. Never
    raises — the caller decides how to react to a non-zero exit code.

    READS ``result.stdout``, NEVER ``result.output`` — and that one word
    is the whole point of this docstring.

    ``result.output`` is stdout and stderr MERGED. So a single line
    written to stderr by any JSON-emitting command makes the payload
    unparseable and hands every MCP caller ``data: None``. Measured on
    click 8.4.2::

        output : 'warning: something\\n[1, 2]\\n'   -> JSONDecodeError
        stdout : '[1, 2]\\n'                       -> parses

    Why that is worse than it sounds, and why it is a mechanism rather
    than a comment (scitex-db, 2026-08-09)::

        data: None    means THE PAYLOAD FAILED TO PARSE
        data: []      means THE QUERY RETURNED NO ROWS
        if not data:  TREATS THEM IDENTICALLY

    Every caller writing the natural falsy check turns a parse FAILURE
    into the confident conclusion "there is nothing there" — the exact
    shape that produced two false P1 data-loss escalations on this fleet
    on 2026-08-09. And the trigger is the most innocuous change in
    software: adding a warning. Whoever adds it sees a passing CLI and a
    passing suite; the break surfaces hours later in an unrelated agent's
    reasoning, as a wrong conclusion rather than an error.

    ``stderr`` is returned ALONGSIDE rather than discarded, so a warning
    stays visible instead of being silently dropped to protect the
    parse. Note that click removed ``CliRunner(mix_stderr=...)``; on this
    version the streams are separated by reading the right attribute,
    which is why the attribute choice is load-bearing and pinned by a
    test.
    """
    from ...cli_pkg._main import main

    runner = CliRunner()
    result = runner.invoke(main, argv, catch_exceptions=False, standalone_mode=False)
    text = result.stdout or ""
    parsed: Any = None
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {
        "exit_code": result.exit_code,
        "data": parsed,
        "stdout": text,
        "stderr": result.stderr or "",
    }


def invoke_cli_text(argv: list[str]) -> dict[str, Any]:
    """Invoke the CLI, returning raw stdout + stderr + exit code (no JSON parse).

    ``stdout`` holds stdout ONLY, matching its own name — it previously
    carried ``result.output``, which is stdout and stderr merged, so a
    field called ``stdout`` returned something else. ``stderr`` is
    returned separately rather than folded in or dropped.
    """
    from ...cli_pkg._main import main

    runner = CliRunner()
    result = runner.invoke(main, argv, catch_exceptions=False, standalone_mode=False)
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
    }


__all__ = ["invoke_cli_json", "invoke_cli_text"]
