"""Tests for MCP transport selection + server construction.

No import patching anywhere: ``build_mcp_server`` takes its server classes as
a parameter, so these exercise the real function against hand-rolled classes
whose constructor signature must match what the SDK actually accepts. That
also means the whole file runs on a Claude-only deployment where
``openai-agents`` is not installed.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._runners._openai_mcp import (
    McpConfigError,
    build_mcp_server,
    resolve_transport,
)


class RecordingServer:
    """Stands in for an ``agents.mcp`` server, recording what it was built with."""

    def __init__(self, params=None, name=None, cache_tools_list=False):
        self.params = params
        self.name = name
        self.cache_tools_list = cache_tools_list


class StdioServer(RecordingServer):
    """Distinct type so a test can assert WHICH transport was chosen."""


class SseServer(RecordingServer):
    """Distinct type so a test can assert WHICH transport was chosen."""


class HttpServer(RecordingServer):
    """Distinct type so a test can assert WHICH transport was chosen."""


@pytest.fixture
def transports():
    """The three server classes, keyed the way the builder looks them up."""
    return {"stdio": StdioServer, "sse": SseServer, "http": HttpServer}


class TestResolveTransport:
    """Shape and an explicit `type` both decide; neither wins wrongly."""

    def test_command_implies_stdio(self):
        # Arrange
        config = {"command": "npx"}
        # Act
        transport = resolve_transport(config)
        # Assert
        assert transport == "stdio"

    def test_url_implies_http(self):
        # Arrange
        config = {"url": "https://example.invalid/mcp"}
        # Act
        transport = resolve_transport(config)
        # Assert
        assert transport == "http"

    def test_explicit_type_beats_shape(self):
        # Arrange: an entry carrying BOTH must follow what it declares
        config = {"type": "sse", "url": "u", "command": "c"}
        # Act
        transport = resolve_transport(config)
        # Assert
        assert transport == "sse"

    @pytest.mark.parametrize("alias", ["streamable-http", "streamable_http"])
    def test_streamable_aliases_normalise_to_http(self, alias):
        # Arrange
        config = {"type": alias, "url": "u"}
        # Act
        transport = resolve_transport(config)
        # Assert
        assert transport == "http"

    def test_type_is_case_insensitive(self):
        # Arrange
        config = {"type": "STDIO", "command": "c"}
        # Act
        transport = resolve_transport(config)
        # Assert
        assert transport == "stdio"

    def test_unknown_declared_type_does_not_fall_back_to_shape(self):
        # Arrange: a typo'd type must NOT be rescued by the presence of
        # `command` — silently running a server the operator did not ask
        # for is worse than refusing.
        config = {"type": "studio", "command": "c"}
        # Act
        transport = resolve_transport(config)
        # Assert
        assert transport == ""

    def test_empty_entry_resolves_to_nothing(self):
        # Arrange
        config = {}
        # Act
        transport = resolve_transport(config)
        # Assert
        assert transport == ""


class TestBuildStdioServer:
    def test_selects_the_stdio_class(self, transports):
        # Arrange
        config = {"command": "uvx"}
        # Act
        server = build_mcp_server("telegrammer", config, transports=transports)
        # Assert
        assert isinstance(server, StdioServer)

    def test_forwards_command_args_and_env(self, transports):
        # Arrange
        config = {"command": "uvx", "args": ["cct"], "env": {"CCT_BOT_TOKEN": "x"}}
        # Act
        server = build_mcp_server("telegrammer", config, transports=transports)
        # Assert
        assert server.params == {
            "command": "uvx",
            "args": ["cct"],
            "env": {"CCT_BOT_TOKEN": "x"},
        }

    def test_omits_absent_optional_keys(self, transports):
        # Arrange: passing args=[]/env={} is NOT the same as omitting them —
        # an explicit empty env means "run with no environment".
        config = {"command": "sac"}
        # Act
        server = build_mcp_server("bare", config, transports=transports)
        # Assert
        assert server.params == {"command": "sac"}

    def test_carries_the_config_key_as_the_server_name(self, transports):
        # Arrange
        config = {"command": "sac"}
        # Act
        server = build_mcp_server("telegrammer", config, transports=transports)
        # Assert
        assert server.name == "telegrammer"

    def test_caches_the_tool_list(self, transports):
        # Arrange: without this the SDK re-fetches the tool list every run,
        # which for a stdio server is a subprocess round trip per turn.
        config = {"command": "c"}
        # Act
        server = build_mcp_server("x", config, transports=transports)
        # Assert
        assert server.cache_tools_list is True

    def test_missing_command_names_the_offending_server(self, transports):
        # Arrange
        config = {"type": "stdio"}

        # Act
        def build():
            return build_mcp_server("telegrammer", config, transports=transports)

        # Assert
        with pytest.raises(McpConfigError, match="telegrammer"):
            build()


class TestBuildNetworkServer:
    def test_url_alone_selects_streamable_http(self, transports):
        # Arrange
        config = {"url": "https://x.invalid"}
        # Act
        server = build_mcp_server("h", config, transports=transports)
        # Assert
        assert isinstance(server, HttpServer)

    def test_declared_sse_selects_the_sse_class(self, transports):
        # Arrange
        config = {"type": "sse", "url": "https://x.invalid"}
        # Act
        server = build_mcp_server("s", config, transports=transports)
        # Assert
        assert isinstance(server, SseServer)

    def test_forwards_headers_when_present(self, transports):
        # Arrange
        config = {"url": "https://x.invalid", "headers": {"Authorization": "Bearer t"}}
        # Act
        server = build_mcp_server("h", config, transports=transports)
        # Assert
        assert server.params["headers"] == {"Authorization": "Bearer t"}

    def test_missing_url_names_the_offending_server(self, transports):
        # Arrange
        config = {"type": "sse"}

        # Act
        def build():
            return build_mcp_server("remote", config, transports=transports)

        # Assert
        with pytest.raises(McpConfigError, match="remote"):
            build()


class TestUnresolvableEntry:
    def test_names_the_offending_server(self, transports):
        # Arrange
        config = {"cmd": "typo", "notes": "x"}

        # Act
        def build():
            return build_mcp_server("mystery", config, transports=transports)

        # Assert
        with pytest.raises(McpConfigError, match="mystery"):
            build()

    def test_reports_the_keys_it_actually_saw(self, transports):
        # Arrange: the message must be actionable on its own — a dropped
        # server otherwise resurfaces as "the agent ignored its tools",
        # with nothing pointing back at the config.
        config = {"cmd": "typo", "notes": "x"}

        # Act
        def build():
            return build_mcp_server("mystery", config, transports=transports)

        # Assert
        with pytest.raises(McpConfigError, match="cmd"):
            build()
