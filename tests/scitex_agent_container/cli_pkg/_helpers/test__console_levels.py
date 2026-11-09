"""Each output style maps to the scitex-logging level its prefix promises."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import scitex_logging

from scitex_agent_container.cli_pkg._helpers._console import _STYLE_TO_LEVEL


def test_red_renders_as_erro_not_fail():
    # Arrange: the doctrine names an unambiguous failure ERRO; FAIL is a
    # different level scitex-logging renders with a different prefix.
    expected = scitex_logging.ERROR
    # Act
    actual = _STYLE_TO_LEVEL["red"]
    # Assert
    assert actual == expected


def test_error_is_an_explicit_alias_for_red():
    # Arrange
    expected = scitex_logging.ERROR
    # Act
    actual = _STYLE_TO_LEVEL["error"]
    # Assert
    assert actual == expected


def test_fail_stays_available_for_a_failed_check():
    # Arrange
    expected = scitex_logging.FAIL
    # Act
    actual = _STYLE_TO_LEVEL["fail"]
    # Assert
    assert actual == expected


def test_success_renders_as_succ():
    # Arrange
    expected = scitex_logging.SUCCESS
    # Act
    actual = _STYLE_TO_LEVEL["success"]
    # Assert
    assert actual == expected


def test_warn_pulls_the_eye_at_warning_level():
    # Arrange
    expected = scitex_logging.WARNING
    # Act
    actual = _STYLE_TO_LEVEL["warn"]
    # Assert
    assert actual == expected


def test_info_is_visible_by_default():
    # Arrange
    expected = scitex_logging.INFO
    # Act
    actual = _STYLE_TO_LEVEL["info"]
    # Assert
    assert actual == expected


def test_dim_is_hidden_below_debug_verbosity():
    # Arrange: detail moved off the default path must not print by default.
    expected = scitex_logging.DEBUG
    # Act
    actual = _STYLE_TO_LEVEL["dim"]
    # Assert
    assert actual == expected


_EMBEDDED_CONSOLE = """
import importlib.util
import logging
import sys
import scitex_logging

root = logging.getLogger()
root.setLevel(logging.INFO)
stream = logging.StreamHandler(sys.stdout)
stream.setFormatter(logging.Formatter('EMBEDDED: %(message)s'))
file = logging.FileHandler(sys.argv[2])
file.setFormatter(logging.Formatter('AUDIT: %(message)s'))
root.handlers[:] = [stream, file]
name = 'scitex_agent_container.cli_pkg._helpers._console'
spec = importlib.util.spec_from_file_location(name, sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.system_msg('[green]first[/green]', style=sys.argv[3])
spec.loader.exec_module(module)
module.system_msg('second', style=sys.argv[3])
scitex_logging.getLogger('scitex_agent_container.runtime_fixture').info('runtime')
"""


@pytest.mark.parametrize(
    "style,prefix",
    [("info", "INFO"), ("success", "SUCC"), ("warn", "WARN"), ("error", "ERRO")],
)
@pytest.mark.parametrize("format_name", ["default", "detailed"])
def test_console_uses_canonical_stderr_without_intercepting_runtime(
    tmp_path, style, prefix, format_name
):
    """Real handlers retain runtime delivery; lifecycle prose appears once."""
    # Arrange
    source = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "scitex_agent_container/cli_pkg/_helpers/_console.py"
    )
    audit = tmp_path / "runtime.log"
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        SCITEX_DIR=str(tmp_path / "state"),
        SCITEX_LOGGING_FORMAT=format_name,
        SCITEX_LOGGING_FORCE_COLOR="0",
        SCITEX_FORCE_COLOR="0",
    )
    env.pop("PYTHONPATH", None)
    # Act
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _EMBEDDED_CONSOLE,
            str(source),
            str(audit),
            style,
        ],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
    )
    label = (
        "[scitex_agent_container.cli_pkg._helpers._console] "
        if format_name == "detailed"
        else ""
    )
    # Assert
    assert (result.returncode, result.stdout, result.stderr, audit.read_text()) == (
        0,
        "EMBEDDED: runtime\n",
        f"{prefix}: {label}first\n{prefix}: {label}second\n",
        "AUDIT: runtime\n",
    )
