"""Tests for the semi-automated ``claude /login`` driver.

Two layers, no mocks:

* Pure detection / redaction functions are exercised with real string
  inputs (the exact shapes a real pane produces).
* ``run_interactive_login`` is driven end-to-end against REAL tmux and a
  REAL fake-``claude`` bash script (a subprocess that emits a known OAuth
  URL and accepts a pasted code) — real subprocess, real tmux, real
  regex. Skipped when the ``tmux`` binary is absent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scitex_agent_container._account.interactive_login import (
    extract_oauth_url,
    is_code_prompt,
    is_login_method_picker,
    is_login_success,
    redact_pane,
    run_interactive_login,
)

_EXPECTED_URL = (
    "https://claude.ai/oauth/authorize?code=true&client_id=fake&state=NONCE123abc"
)

# A real bash stand-in for ``claude``: prints the input-ready marker, then
# reacts to ``/login`` (login picker) → ``1`` (OAuth URL + code prompt) →
# any code (login success). Drives the whole flow through real tmux.
_FAKE_CLAUDE = f"""\
echo "Fake Claude Code"
echo "? for shortcuts"
while IFS= read -r line; do
  if [ "$line" = "/login" ]; then
    echo "Select login method:"
    echo "  1. Claude account with subscription"
    echo "  2. Anthropic Console account"
  elif [ "$line" = "1" ]; then
    echo "Visit: {_EXPECTED_URL}"
    echo "Paste code here if prompted >"
  elif [ -n "$line" ]; then
    echo "Login successful!"
  fi
done
"""


def _write_fake_claude(directory: Path) -> Path:
    """Write the fake-claude bash script into ``directory`` and return it."""
    script = directory / "fake-claude.sh"
    script.write_text(_FAKE_CLAUDE, encoding="utf-8")
    return script


def test_extract_oauth_url_returns_claude_ai_authorize_url() -> None:
    # Arrange
    pane = f"Open your browser: {_EXPECTED_URL}\nwaiting..."
    # Act
    found = extract_oauth_url(pane)
    # Assert
    assert found == _EXPECTED_URL


def test_extract_oauth_url_returns_platform_claude_host_url() -> None:
    # Arrange
    url = "https://platform.claude.com/oauth/authorize?x=1&state=zz"
    pane = f"visit {url} now"
    # Act
    found = extract_oauth_url(pane)
    # Assert
    assert found == url


def test_extract_oauth_url_ignores_unrelated_https_url() -> None:
    # Arrange
    pane = "some log line https://example.com/oauth/authorize?x=1 not ours"
    # Act
    found = extract_oauth_url(pane)
    # Assert
    assert found is None


def test_is_code_prompt_ignores_oauth_url_code_param() -> None:
    # Arrange
    pane = f"Visit: {_EXPECTED_URL}"
    # Act
    detected = is_code_prompt(pane)
    # Assert
    assert detected is False


def test_is_code_prompt_detects_paste_code_marker() -> None:
    # Arrange
    pane = "Paste code here if prompted >"
    # Act
    detected = is_code_prompt(pane)
    # Assert
    assert detected is True


def test_is_login_success_detects_login_successful_marker() -> None:
    # Arrange
    pane = "Login successful! Welcome back."
    # Act
    detected = is_login_success(pane)
    # Assert
    assert detected is True


def test_is_login_method_picker_detects_select_method_marker() -> None:
    # Arrange
    pane = "Select login method:\n  1. Claude account with subscription"
    # Act
    detected = is_login_method_picker(pane)
    # Assert
    assert detected is True


def test_redact_pane_masks_anthropic_api_key_token() -> None:
    # Arrange
    pane = "leaked sk-ant-SECRETVALUE0123456789 in the pane"
    # Act
    cleaned = redact_pane(pane)
    # Assert
    assert "sk-ant-SECRETVALUE0123456789" not in cleaned


def test_redact_pane_masks_supplied_extra_secret_value() -> None:
    # Arrange
    pane = "the code was AUTHCODE-xyz-999 typed in"
    # Act
    cleaned = redact_pane(pane, "AUTHCODE-xyz-999")
    # Assert
    assert "AUTHCODE-xyz-999" not in cleaned


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux binary not on PATH")
def test_run_interactive_login_extracts_oauth_url_from_pane(tmp_path: Path) -> None:
    # Arrange
    fake = _write_fake_claude(tmp_path)
    code_file = tmp_path / "code.txt"
    code_file.write_text("auth-code-abc123\n", encoding="utf-8")
    # Act
    result = run_interactive_login(
        "testacct",
        notify=False,
        code_file=str(code_file),
        claude_bin=f"bash {fake}",
        workdir=str(tmp_path),
        url_timeout_s=20.0,
        human_timeout_s=20.0,
        poll_s=0.3,
        echo=lambda *a, **k: None,
    )
    # Assert
    assert result.url == _EXPECTED_URL


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux binary not on PATH")
def test_run_interactive_login_completes_via_code_file_drop(tmp_path: Path) -> None:
    # Arrange
    fake = _write_fake_claude(tmp_path)
    code_file = tmp_path / "code.txt"
    code_file.write_text("auth-code-abc123\n", encoding="utf-8")
    # Act
    result = run_interactive_login(
        "testacct",
        notify=False,
        code_file=str(code_file),
        claude_bin=f"bash {fake}",
        workdir=str(tmp_path),
        url_timeout_s=20.0,
        human_timeout_s=20.0,
        poll_s=0.3,
        echo=lambda *a, **k: None,
    )
    # Assert
    assert result.status == "success"
