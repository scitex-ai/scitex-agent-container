"""``_run_child`` must return the output, not die on it.

THE BUG (operator, 2026-08-11). ``host_exec_local`` answered HTTP 500 instead
of returning a log. The child ran fine; the DECODE killed it. ``_run_child``
opened the pipes with a bare ``text=True``, which decodes STRICTLY, so one byte
the codec disliked raised ``UnicodeDecodeError`` out of ``communicate()`` and
the caller was handed an error in place of the bytes it asked for.

The inputs that trigger it are the ordinary ones, which is what makes it worth
a test module rather than a one-line patch:

  * a log carrying ANSI colour written under a non-UTF-8 locale,
  * output TRUNCATED mid multibyte character — exactly what the timeout kill
    and the bounded post-kill drain in this same module produce,
  * anything faintly binary (a core-dump path, a tarball listing).

So the failure lands hardest on precisely the runs somebody is already trying
to debug. A diagnostic tool that dies on diagnostic output is the wrong failure
mode.

PS-204 / no-mocks: these drive the REAL ``_run_child`` against REAL child
processes that write REAL undecodable bytes to their real pipes. Nothing here
stands in for the behaviour under test — the whole defect lived in how the pipe
was opened, so a stubbed pipe would have tested nothing.
"""

from __future__ import annotations

import sys

from scitex_agent_container._listen._host_exec_child import _run_child

# The replacement character a lenient decoder substitutes for an undecodable
# byte. Asserting on it is what proves the bytes were REPLACED rather than the
# stream silently truncated at the first bad byte.
_REPLACEMENT = "�"

# A lone 0xFF is not valid UTF-8 in any position; 0xE3 0x81 opens a 3-byte
# sequence that is then cut short, which is the truncation shape a killed child
# leaves behind.
_LONE_INVALID_BYTE = rb"b'before\xffafter'"
_TRUNCATED_MULTIBYTE = rb"b'head\xe3\x81'"


def _emit(expr: bytes, *, stream: str = "stdout") -> list[str]:
    """argv for a child that writes raw ``expr`` bytes to ``stream``."""
    return [
        sys.executable,
        "-c",
        f"import sys; sys.{stream}.buffer.write({expr.decode()}); "
        f"sys.{stream}.buffer.flush()",
    ]


def _run(argv: list[str]):
    return _run_child(argv, cwd=None, child_timeout_s=30.0, env=None)


# ---------------------------------------------------------------------------
# Undecodable stdout — returned, never raised
# ---------------------------------------------------------------------------


def test_undecodable_stdout_does_not_raise() -> None:
    """The whole defect: this call used to raise instead of returning."""
    # Arrange
    argv = _emit(_LONE_INVALID_BYTE)
    # Act
    outcome = _run(argv)
    # Assert
    assert outcome.exit_code == 0


def test_undecodable_stdout_keeps_the_text_before_the_bad_byte() -> None:
    # Arrange
    argv = _emit(_LONE_INVALID_BYTE)
    # Act
    stdout = _run(argv).stdout
    # Assert
    assert "before" in stdout


def test_undecodable_stdout_keeps_the_text_after_the_bad_byte() -> None:
    """A bad byte must cost ONE character, not the rest of the log."""
    # Arrange
    argv = _emit(_LONE_INVALID_BYTE)
    # Act
    stdout = _run(argv).stdout
    # Assert
    assert "after" in stdout


def test_undecodable_stdout_substitutes_a_replacement_character() -> None:
    """Visible, not silent: the operator can see a byte was unrepresentable."""
    # Arrange
    argv = _emit(_LONE_INVALID_BYTE)
    # Act
    stdout = _run(argv).stdout
    # Assert
    assert _REPLACEMENT in stdout


# ---------------------------------------------------------------------------
# Truncated multibyte — the shape a killed / drained child leaves behind
# ---------------------------------------------------------------------------


def test_output_cut_mid_multibyte_character_still_returns() -> None:
    # Arrange
    argv = _emit(_TRUNCATED_MULTIBYTE)
    # Act
    outcome = _run(argv)
    # Assert
    assert outcome.exit_code == 0


def test_output_cut_mid_multibyte_character_keeps_the_decodable_head() -> None:
    # Arrange
    argv = _emit(_TRUNCATED_MULTIBYTE)
    # Act
    stdout = _run(argv).stdout
    # Assert
    assert "head" in stdout


# ---------------------------------------------------------------------------
# stderr takes the same pipe options, so it needs the same guarantee
# ---------------------------------------------------------------------------


def test_undecodable_stderr_does_not_raise() -> None:
    # Arrange
    argv = _emit(_LONE_INVALID_BYTE, stream="stderr")
    # Act
    outcome = _run(argv)
    # Assert
    assert outcome.exit_code == 0


def test_undecodable_stderr_keeps_its_surrounding_text() -> None:
    # Arrange
    argv = _emit(_LONE_INVALID_BYTE, stream="stderr")
    # Act
    stderr = _run(argv).stderr
    # Assert
    assert "before" in stderr and "after" in stderr


# ---------------------------------------------------------------------------
# The ordinary path is untouched — a lenient decoder must not alter clean text
# ---------------------------------------------------------------------------


def test_clean_utf8_output_is_returned_verbatim() -> None:
    """Non-ASCII that IS valid UTF-8 must survive intact, not be mangled."""
    # Arrange — the pinned encoding is what makes this hold under any locale.
    argv = [sys.executable, "-c", "print('日本語 — ok')"]
    # Act
    stdout = _run(argv).stdout
    # Assert
    assert "日本語 — ok" in stdout
