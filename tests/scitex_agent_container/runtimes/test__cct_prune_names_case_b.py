"""A pruned agent that ASKED for the Telegram rail must not log "intentional".

The regression these guard, measured 2026-08-10: scitex-agent-container-04,
scitex-app, scitex-db and scitex-priv-setup each declared
``server:claude-code-telegrammer`` in their spec, had the MCP server silently
removed, and logged only the case-(A) INFO — "this is the intentional no-bot
path, not an error" — while the operator concluded his agents were ignoring
him. 91 of 102 fleet specs declare the channel.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._cct_token_pool import (
    prune_tokenless_telegrammer_mcp,
)

_CHANNEL = "server:claude-code-telegrammer"
_KEY = "claude-code-telegrammer"


def _spec(channels):
    return SimpleNamespace(
        name="scitex-agent-container-04",
        workdir="/home/ywatanabe/proj/scitex-agent-container",
        env={},
        claude=SimpleNamespace(channels=list(channels)),
    )


@pytest.fixture
def dest(tmp_path):
    """A materialised home with the telegrammer entry and NO token."""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {_KEY: {"command": "bun"}, "other": {}}}) + "\n"
    )
    (tmp_path / ".env").write_text("SOMETHING=else\n")
    return tmp_path


class TestRequestedButUnresolved:
    def test_entry_is_removed(self, dest):
        # Arrange
        config = _spec([_CHANNEL])
        # Act
        prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert _KEY not in json.loads((dest / ".mcp.json").read_text())["mcpServers"]

    def test_it_logs_at_error_not_info(self, dest, caplog):
        # Arrange
        config = _spec([_CHANNEL])
        # Act
        with caplog.at_level(logging.ERROR):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_it_does_not_call_the_removal_intentional(self, dest, caplog):
        # Arrange
        # Matches the case-(A) sentence in FULL. A bare "intentional no-bot
        # path" substring is present in the case-(B) text too — it says "this is
        # NOT the intentional no-bot path" — so the short form passes against
        # the old code and fails against the new one, which is backwards.
        config = _spec([_CHANNEL])
        # Act
        with caplog.at_level(logging.INFO):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert "intentional no-bot path, not an error" not in caplog.text

    def test_it_says_the_agent_is_mute_and_deaf(self, dest, caplog):
        # Arrange
        config = _spec([_CHANNEL])
        # Act
        with caplog.at_level(logging.ERROR):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert "MUTE AND DEAF" in caplog.text

    def test_it_names_the_agent(self, dest, caplog):
        # Arrange
        config = _spec([_CHANNEL])
        # Act
        with caplog.at_level(logging.ERROR):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert "scitex-agent-container-04" in caplog.text

    def test_it_names_the_slots_it_tried(self, dest, caplog):
        # Arrange
        config = _spec([_CHANNEL])
        # Act
        with caplog.at_level(logging.ERROR):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert "CCT_BOT_TOKEN_SCITEX_AGENT_CONTAINER_04" in caplog.text


class TestBotlessByDesign:
    def test_entry_is_still_removed(self, dest):
        # Arrange
        config = _spec([])
        # Act
        prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert _KEY not in json.loads((dest / ".mcp.json").read_text())["mcpServers"]

    def test_it_stays_informational(self, dest, caplog):
        # Arrange
        config = _spec([])
        # Act
        with caplog.at_level(logging.INFO):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_it_still_says_intentional(self, dest, caplog):
        # Arrange
        config = _spec([])
        # Act
        with caplog.at_level(logging.INFO):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert "intentional no-bot path" in caplog.text


class TestBackCompat:
    def test_no_config_still_prunes(self, dest):
        # Arrange
        # The old one-argument call site.
        # Act
        removed = prune_tokenless_telegrammer_mcp(dest)
        # Assert
        assert removed is True

    def test_no_config_does_not_claim_misconfiguration(self, dest, caplog):
        # Arrange
        # Without a spec the function cannot tell (A) from (B); silence about
        # which one it is beats guessing wrong in either direction.
        # Act
        with caplog.at_level(logging.INFO):
            prune_tokenless_telegrammer_mcp(dest)
        # Assert
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestAgentWithAToken:
    def test_entry_is_kept(self, dest):
        # Arrange
        (dest / ".env").write_text("CCT_BOT_TOKEN=abc123\n")
        config = _spec([_CHANNEL])
        # Act
        prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert _KEY in json.loads((dest / ".mcp.json").read_text())["mcpServers"]

    def test_nothing_is_logged_as_an_error(self, dest, caplog):
        # Arrange
        (dest / ".env").write_text("CCT_BOT_TOKEN=abc123\n")
        config = _spec([_CHANNEL])
        # Act
        with caplog.at_level(logging.INFO):
            prune_tokenless_telegrammer_mcp(dest, config)
        # Assert
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
