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


def test_missing_token_errors_clearly(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
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
    assert result.exit_code != 0
    assert "No sac-listen token" in result.output


def test_happy_path_wraps_in_channel_tag(token_file):
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

    assert result.exit_code == 0, result.output
    # URL is the per-agent send endpoint
    assert captured["url"] == "http://127.0.0.1:7878/agents/alpha/send"
    # Body has type=prompt and channel-wrapped payload
    assert captured["body"]["type"] == "prompt"
    p = captured["body"]["prompt"]
    assert p.startswith('<channel source="sac" from="quality-orchestrator">')
    assert "hello there" in p
    assert p.endswith("</channel>")
    # Bearer token forwarded
    assert captured["auth"] == "Bearer test-token"


def test_unreachable_listen_explains(token_file):
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

    assert result.exit_code != 0
    assert "unreachable" in result.output
    assert "Is `sac listen` running" in result.output
