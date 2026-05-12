"""Tests for ``sac channel send`` — local agent-to-agent messaging."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.channel_cmds import send


@pytest.fixture
def token_file(tmp_path: Path):
    tf = tmp_path / "tok"
    tf.write_text("test-token", encoding="utf-8")
    return tf


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
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"name": "alpha", "returncode": 0, "stdout": "ok"}
    ).encode()
    fake_resp.__enter__ = lambda s: fake_resp
    fake_resp.__exit__ = lambda *args: False
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return fake_resp

    with patch(
        "scitex_agent_container.cli_pkg.channel_cmds.urllib.request.urlopen",
        side_effect=fake_urlopen,
    ):
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
    assert captured["url"] == "http://127.0.0.1:7878/v1/sac/agents/alpha/send"
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

    with patch(
        "scitex_agent_container.cli_pkg.channel_cmds.urllib.request.urlopen",
        side_effect=boom,
    ):
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
