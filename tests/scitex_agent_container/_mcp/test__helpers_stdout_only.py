"""A stderr warning must never make an MCP payload unparseable.

INCIDENT-ADJACENT, 2026-08-09. ``invoke_cli_json`` read Click's
``result.output``, which is stdout and stderr MERGED. One line written to
stderr by any JSON-emitting sac command therefore made its payload
unparseable and handed every MCP caller ``data: None``.

Found by implementing it: adding a ``store:`` provenance line to stderr
for ``db query --json`` broke an existing test. Measured on click 8.4.2::

    output : 'warning: something\\n[1, 2]\\n'   -> JSONDecodeError
    stdout : '[1, 2]\\n'                       -> parses

Why it is urgent rather than cosmetic — scitex-db's sharpening::

    data: None    means THE PAYLOAD FAILED TO PARSE
    data: []      means THE QUERY RETURNED NO ROWS
    if not data:  TREATS THEM IDENTICALLY

Every caller writing the natural falsy check converts a parse failure
into "there is nothing there" — the same indistinguishability that made
three agents conclude the fleet registry had been wiped, two escalating
it as P1 data loss. This trap manufactures that on demand for any sac
JSON command, and its trigger is the most innocuous change in software:
adding a warning.

So the guard is mechanical, not documentary. A comment saying "do not
write to stderr" is the mechanism that already failed four times in one
day across four surfaces.

No mocks (PA-306): a real Click command through the real runner. AAA
markers, one assertion per test.
"""

from __future__ import annotations

import json

import click
from click.testing import CliRunner


@click.command()
def _noisy() -> None:
    """Emits a stderr warning AND a JSON payload — the exact hazard."""
    click.echo("warning: deprecated flag", err=True)
    click.echo(json.dumps([{"id": 1}]))


def _invoke_like_the_helper(cmd) -> dict:
    """Mirror invoke_cli_json's stream handling against an arbitrary command.

    The helper hard-codes sac's own ``main``, so the contract is exercised
    through an equivalent invocation rather than by shelling the whole CLI
    — the behaviour under test is WHICH STREAM IS PARSED, not sac's argv.
    """
    result = CliRunner().invoke(cmd, [], catch_exceptions=False)
    text = result.stdout or ""
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return {"data": parsed, "stdout": text, "stderr": result.stderr or ""}


def test_a_stderr_warning_does_not_break_the_payload():
    # Arrange: the regression guard. Parsing result.output instead of
    # result.stdout makes this None, which every `if not data` caller
    # then reads as "no rows".
    cmd = _noisy
    # Act
    out = _invoke_like_the_helper(cmd)
    # Assert
    assert out["data"] == [{"id": 1}]


def test_the_warning_is_still_visible():
    # Arrange: separating the streams must not SWALLOW the warning —
    # protecting the parse by discarding diagnostics would trade one
    # silent failure for another.
    cmd = _noisy
    # Act
    out = _invoke_like_the_helper(cmd)
    # Assert
    assert "deprecated flag" in out["stderr"]


def test_stdout_field_holds_stdout_only():
    # Arrange: the field is named `stdout`; it previously carried the
    # merged stream, so its name was a lie.
    cmd = _noisy
    # Act
    out = _invoke_like_the_helper(cmd)
    # Assert
    assert "warning:" not in out["stdout"]


def test_click_result_output_really_is_merged():
    # Arrange: pins the upstream behaviour this whole fix rests on. If a
    # future click makes `output` stdout-only, this fails and tells the
    # reader the hazard is gone rather than leaving the guard as cargo.
    cmd = _noisy
    # Act
    result = CliRunner().invoke(cmd, [], catch_exceptions=False)
    # Assert
    assert "warning:" in result.output


def test_the_real_helper_returns_stderr_separately():
    # Arrange: the shipped helper, not the mirror — its contract now has
    # four keys and callers may branch on stderr.
    from scitex_agent_container._mcp._tools._helpers import invoke_cli_json

    # Act
    out = invoke_cli_json(["--help"])
    # Assert
    assert "stderr" in out
