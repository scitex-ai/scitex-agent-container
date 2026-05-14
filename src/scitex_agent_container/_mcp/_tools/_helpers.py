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
    its stdout as JSON.

    Returns ``{"exit_code", "data", "stdout"}``. ``data`` is the parsed
    JSON when stdout decoded cleanly; ``None`` otherwise (with the raw
    text in ``stdout``). Never raises — caller decides how to react to
    a non-zero exit code.
    """
    from ...cli_pkg._main import main

    runner = CliRunner()
    result = runner.invoke(main, argv, catch_exceptions=False, standalone_mode=False)
    text = result.output or ""
    parsed: Any = None
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {"exit_code": result.exit_code, "data": parsed, "stdout": text}


def invoke_cli_text(argv: list[str]) -> dict[str, Any]:
    """Invoke the CLI, returning raw stdout + exit code (no JSON parse)."""
    from ...cli_pkg._main import main

    runner = CliRunner()
    result = runner.invoke(main, argv, catch_exceptions=False, standalone_mode=False)
    return {"exit_code": result.exit_code, "stdout": result.output or ""}


__all__ = ["invoke_cli_json", "invoke_cli_text"]
