"""Tests for ``sac channel send`` — local agent-to-agent messaging.

PA-306: no `unittest.mock`. The HTTP layer is replaced with a real
hand-rolled fake `urlopen` callable that records the request and
returns a real `io.BytesIO` response. The fake is swapped onto
``channel_group.urllib.request.urlopen`` via explicit save/restore.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.channel_group as cg_mod
from scitex_agent_container.cli_pkg.channel_group import send


@pytest.fixture
def token_file(tmp_path: Path):
    tf = tmp_path / "tok"
    tf.write_text("test-token", encoding="utf-8")
    return tf


@contextmanager
def _swap_urlopen(fn: Callable) -> Iterator[None]:
    """Replace the channel_group's urlopen with a real callable."""
    saved = cg_mod.urllib.request.urlopen
    cg_mod.urllib.request.urlopen = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        cg_mod.urllib.request.urlopen = saved  # type: ignore[assignment]


class _FakeResp:
    """Minimal context-manager response stand-in. Real I/O via BytesIO."""

    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a) -> None:
        return None

    def read(self) -> bytes:
        return self._buf.getvalue()


# ---------------------------------------------------------------------------
# Scenario fixtures — run each CLI invocation once and let per-behaviour
# tests assert on a single facet of the captured outcome.
# ---------------------------------------------------------------------------


@pytest.fixture
def missing_token_result(tmp_path: Path):
    runner = CliRunner()
    return runner.invoke(
        send,
        [
            "alpha",
            "hi",
            "--token-file",
            str(tmp_path / "absent"),
            "--listen-url",
            "http://127.0.0.1:1",
        ],
    )


@pytest.fixture
def happy_path_outcome(token_file) -> dict:
    runner = CliRunner()
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp(
            json.dumps({"name": "alpha", "returncode": 0, "stdout": "ok"}).encode()
        )

    with _swap_urlopen(fake_urlopen):
        result = runner.invoke(
            send,
            [
                "alpha",
                "hello there",
                "--from",
                "quality-orchestrator",
                "--token-file",
                str(token_file),
                "--listen-url",
                "http://127.0.0.1:7878",
            ],
        )

    return {"result": result, "captured": captured}


@pytest.fixture
def unreachable_listen_result(token_file):
    runner = CliRunner()
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with _swap_urlopen(boom):
        result = runner.invoke(
            send,
            [
                "alpha",
                "hi",
                "--token-file",
                str(token_file),
                "--listen-url",
                "http://127.0.0.1:7878",
            ],
        )

    return result


# ---------------------------------------------------------------------------
# Missing token --> command must fail with an explanatory message
# ---------------------------------------------------------------------------


def test_missing_token_file_makes_send_exit_nonzero(missing_token_result) -> None:
    # Arrange
    result = missing_token_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code != 0


def test_missing_token_file_prints_no_sac_listen_token_message(
    missing_token_result,
) -> None:
    # Arrange
    result = missing_token_result
    # Act
    output = result.output
    # Assert
    assert "No sac-listen token" in output


# ---------------------------------------------------------------------------
# Happy path --> CLI exits 0 and request body is correctly framed
# ---------------------------------------------------------------------------


def test_send_happy_path_exits_zero(happy_path_outcome: dict) -> None:
    # Arrange
    outcome = happy_path_outcome
    # Act
    result = outcome["result"]
    # Assert
    assert result.exit_code == 0, result.output


def test_send_targets_per_agent_send_endpoint_url(happy_path_outcome: dict) -> None:
    # Arrange
    outcome = happy_path_outcome
    # Act
    url = outcome["captured"]["url"]
    # Assert
    assert url == "http://127.0.0.1:7878/agents/alpha/send"


def test_send_request_body_type_is_prompt(happy_path_outcome: dict) -> None:
    # Arrange
    outcome = happy_path_outcome
    # Act
    body_type = outcome["captured"]["body"]["type"]
    # Assert
    assert body_type == "prompt"


def test_send_prompt_opens_with_channel_tag(happy_path_outcome: dict) -> None:
    # Arrange
    outcome = happy_path_outcome
    # Act
    prompt = outcome["captured"]["body"]["prompt"]
    # Assert
    assert prompt.startswith('<channel source="sac" from="quality-orchestrator">')


def test_send_prompt_contains_user_message_text(happy_path_outcome: dict) -> None:
    # Arrange
    outcome = happy_path_outcome
    # Act
    prompt = outcome["captured"]["body"]["prompt"]
    # Assert
    assert "hello there" in prompt


def test_send_prompt_closes_with_channel_end_tag(happy_path_outcome: dict) -> None:
    # Arrange
    outcome = happy_path_outcome
    # Act
    prompt = outcome["captured"]["body"]["prompt"]
    # Assert
    assert prompt.endswith("</channel>")


def test_send_forwards_token_as_bearer_authorization_header(
    happy_path_outcome: dict,
) -> None:
    # Arrange
    outcome = happy_path_outcome
    # Act
    auth = outcome["captured"]["auth"]
    # Assert
    assert auth == "Bearer test-token"


# ---------------------------------------------------------------------------
# Listen URL unreachable --> command must explain the failure
# ---------------------------------------------------------------------------


def test_unreachable_listen_url_makes_send_exit_nonzero(
    unreachable_listen_result,
) -> None:
    # Arrange
    result = unreachable_listen_result
    # Act
    exit_code = result.exit_code
    # Assert
    assert exit_code != 0


def test_unreachable_listen_url_output_contains_unreachable(
    unreachable_listen_result,
) -> None:
    # Arrange
    result = unreachable_listen_result
    # Act
    output = result.output
    # Assert
    assert "unreachable" in output


def test_unreachable_listen_url_output_suggests_checking_sac_listen(
    unreachable_listen_result,
) -> None:
    # Arrange
    result = unreachable_listen_result
    # Act
    output = result.output
    # Assert
    assert "Is `sac listen` running" in output
