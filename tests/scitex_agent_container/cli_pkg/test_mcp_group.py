"""Tests for ``sac mcp`` group — start / doctor / list-tools / install.

No-mocks rewrite (PA-306). The previous version monkeypatched
``sys.modules`` to fabricate a ``scitex_agent_container._mcp`` package
populated with ``MagicMock`` callables — fake-for-fake, untrustworthy.
This version:

* exercises the public loader seam (``mcp_group._load_run_server`` /
  ``_load_get_server`` / ``_load_fastmcp_version``) by swapping in
  hand-rolled real callables that return real-behaviour objects (same
  save/restore pattern as ``test_image_group``),
* deletes tests whose only assertion was ``MagicMock.assert_called_once()``
  (mock-only behaviour, no real-world counterpart),
* keeps tests that exercise the real ``_enumerate_tools`` shape-detection
  paths against hand-rolled real classes (no MagicMock).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Iterator

from click.testing import CliRunner

from scitex_agent_container.cli_pkg import mcp_group as mg
from scitex_agent_container.cli_pkg.mcp_group import mcp

# ---------------------------------------------------------------------------
# Real-fake loaders — hand-rolled callables that record their calls and
# return real-behaviour values. Stand in for the optional ``_mcp`` package
# and ``fastmcp`` without ``MagicMock``.
# ---------------------------------------------------------------------------


class _FakeRunServer:
    """Real callable recording each ``run_server(...)`` invocation."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, transport: str, host: str, port: int) -> None:
        self.calls.append({"transport": transport, "host": host, "port": port})


class _FakeTool:
    """Real-shape Tool object exposing ``name`` and ``description``."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description


class _FakeServer:
    """Real-shape MCP server whose ``list_tools()`` returns concrete tools."""

    def __init__(self, tools: list[_FakeTool]) -> None:
        self._tools_list = tools

    async def list_tools(self) -> list[_FakeTool]:
        return list(self._tools_list)


@contextmanager
def _use_run_server(fake: _FakeRunServer) -> Iterator[_FakeRunServer]:
    """Swap ``mcp_group._load_run_server`` for a loader returning ``fake``."""
    saved = mg._load_run_server
    mg._load_run_server = lambda: fake
    try:
        yield fake
    finally:
        mg._load_run_server = saved


@contextmanager
def _use_run_server_import_error() -> Iterator[None]:
    """Swap ``_load_run_server`` for a callable that raises ``ImportError``."""
    saved = mg._load_run_server

    def _raise():
        raise ImportError("no fastmcp")

    mg._load_run_server = _raise
    try:
        yield
    finally:
        mg._load_run_server = saved


@contextmanager
def _use_get_server(server: object) -> Iterator[object]:
    """Swap ``_load_get_server`` to return a loader yielding ``server``."""
    saved = mg._load_get_server
    mg._load_get_server = lambda: lambda: server
    try:
        yield server
    finally:
        mg._load_get_server = saved


@contextmanager
def _use_get_server_import_error() -> Iterator[None]:
    saved = mg._load_get_server

    def _raise():
        raise ImportError("fastmcp missing")

    mg._load_get_server = _raise
    try:
        yield
    finally:
        mg._load_get_server = saved


@contextmanager
def _use_get_server_raises(exc: BaseException) -> Iterator[None]:
    """Swap ``_load_get_server`` to raise a non-Import exception."""
    saved = mg._load_get_server

    def _raise():
        raise exc

    mg._load_get_server = _raise
    try:
        yield
    finally:
        mg._load_get_server = saved


@contextmanager
def _use_fastmcp_version(version: str) -> Iterator[None]:
    saved = mg._load_fastmcp_version
    mg._load_fastmcp_version = lambda: version
    try:
        yield
    finally:
        mg._load_fastmcp_version = saved


@contextmanager
def _use_fastmcp_missing() -> Iterator[None]:
    saved = mg._load_fastmcp_version

    def _raise():
        raise ImportError("no fastmcp")

    mg._load_fastmcp_version = _raise
    try:
        yield
    finally:
        mg._load_fastmcp_version = saved


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_dry_run_stdio_prints_stdio_transport():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(mcp, ["start", "--dry-run"])
    # Assert
    assert result.exit_code == 0 and "transport=stdio" in result.output


def test_start_dry_run_http_prints_http_transport_and_port():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        mcp,
        ["start", "--dry-run", "--http", "--host", "0.0.0.0", "--port", "9999"],
    )
    # Assert
    assert (
        result.exit_code == 0
        and "transport=http" in result.output
        and "9999" in result.output
    )


def test_start_invokes_run_server_with_stdio_defaults():
    # Arrange
    fake = _FakeRunServer()
    runner = CliRunner()
    # Act
    with _use_run_server(fake):
        result = runner.invoke(mcp, ["start"])
    # Assert
    assert result.exit_code == 0 and fake.calls == [
        {"transport": "stdio", "host": "127.0.0.1", "port": 8_970}
    ]


def test_start_http_prints_url_and_passes_port_through_to_runner():
    # Arrange
    fake = _FakeRunServer()
    runner = CliRunner()
    # Act
    with _use_run_server(fake):
        result = runner.invoke(mcp, ["start", "--http", "--port", "1234"])
    # Assert
    assert (
        result.exit_code == 0
        and "http://127.0.0.1:1234" in result.output
        and fake.calls[0]["transport"] == "http"
        and fake.calls[0]["port"] == 1_234
    )


def test_start_surfaces_fastmcp_import_error_with_install_hint():
    # Arrange
    runner = CliRunner()
    # Act
    with _use_run_server_import_error():
        result = runner.invoke(mcp, ["start"])
    # Assert
    assert result.exit_code != 0 and "fastmcp" in result.output


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_ok_reports_fastmcp_version_and_server_ready():
    # Arrange
    server = _FakeServer([_FakeTool("a"), _FakeTool("b")])
    runner = CliRunner()
    # Act
    with _use_fastmcp_version("9.9.9"), _use_get_server(server):
        result = runner.invoke(mcp, ["doctor"])
    # Assert
    assert (
        result.exit_code == 0
        and "fastmcp" in result.output
        and "MCP server ready" in result.output
    )


def test_doctor_missing_fastmcp_exits_nonzero_with_install_hint():
    # Arrange
    runner = CliRunner()
    # Act
    with _use_fastmcp_missing():
        result = runner.invoke(mcp, ["doctor"])
    # Assert
    assert result.exit_code != 0 and "fastmcp not installed" in result.output


def test_doctor_reports_server_error_when_registration_raises():
    # Arrange
    runner = CliRunner()
    # Act
    with (
        _use_fastmcp_version("1.0"),
        _use_get_server_raises(RuntimeError("registration failed")),
    ):
        result = runner.invoke(mcp, ["doctor"])
    # Assert
    assert result.exit_code != 0 and "MCP server error" in result.output


# ---------------------------------------------------------------------------
# list-tools
# ---------------------------------------------------------------------------


def test_list_tools_human_renders_each_tool_name_and_first_desc_line():
    # Arrange
    server = _FakeServer([_FakeTool("foo", "Foo tool\nlong"), _FakeTool("bar", "")])
    runner = CliRunner()
    # Act
    with _use_get_server(server):
        result = runner.invoke(mcp, ["list-tools"])
    # Assert
    assert (
        result.exit_code == 0
        and "foo" in result.output
        and "bar" in result.output
        and "Foo tool" in result.output
    )


def test_list_tools_json_emits_count_and_sorted_tool_names():
    # Arrange
    server = _FakeServer([_FakeTool("z"), _FakeTool("a")])
    runner = CliRunner()
    # Act
    with _use_get_server(server):
        result = runner.invoke(mcp, ["list-tools", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert (
        result.exit_code == 0
        and payload["count"] == 2
        and [t["name"] for t in payload["tools"]] == ["a", "z"]
    )


def test_list_tools_import_error_json_payload_reports_zero_and_error():
    # Arrange
    runner = CliRunner()
    # Act
    with _use_get_server_import_error():
        result = runner.invoke(mcp, ["list-tools", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert (
        result.exit_code == 0
        and payload["count"] == 0
        and "fastmcp" in payload["error"]
    )


def test_list_tools_import_error_human_prints_install_hint():
    # Arrange
    runner = CliRunner()
    # Act
    with _use_get_server_import_error():
        result = runner.invoke(mcp, ["list-tools"])
    # Assert
    assert result.exit_code == 0 and "fastmcp not installed" in result.output


# ---------------------------------------------------------------------------
# install (pure print, no backend)
# ---------------------------------------------------------------------------


def test_install_default_prints_pip_install_instructions():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(mcp, ["install"])
    # Assert
    assert (
        result.exit_code == 0
        and "Installation" in result.output
        and "pip install" in result.output
    )


def test_install_claude_code_emits_mcp_config_snippet():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(mcp, ["install", "--claude-code"])
    # Assert
    assert (
        result.exit_code == 0
        and '"scitex-agent-container"' in result.output
        and '"sac"' in result.output
    )


# ---------------------------------------------------------------------------
# channel — wake-on-push turn-url passthrough (WI-1)
# ---------------------------------------------------------------------------


class _FakeChannelMain:
    """Real callable recording each ``channel.main(...)`` invocation."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, name=None, listen_url=None, turn_url=None) -> None:
        self.calls.append(
            {"name": name, "listen_url": listen_url, "turn_url": turn_url}
        )


@contextmanager
def _use_channel_main(fake: _FakeChannelMain) -> Iterator[_FakeChannelMain]:
    """Swap ``mcp_group._load_channel_main`` for a loader returning ``fake``."""
    saved = mg._load_channel_main
    mg._load_channel_main = lambda: fake
    try:
        yield fake
    finally:
        mg._load_channel_main = saved


def test_channel_forwards_turn_url_to_main():
    # Arrange
    fake = _FakeChannelMain()
    runner = CliRunner()
    # Act
    with _use_channel_main(fake):
        result = runner.invoke(
            mcp,
            ["channel", "--name", "lead", "--turn-url", "http://127.0.0.1:9/v1/turn"],
        )
    # Assert
    assert result.exit_code == 0 and fake.calls[0]["turn_url"] == (
        "http://127.0.0.1:9/v1/turn"
    )


def test_channel_turn_url_defaults_to_none():
    # Arrange
    fake = _FakeChannelMain()
    runner = CliRunner()
    # Act
    with _use_channel_main(fake):
        result = runner.invoke(mcp, ["channel", "--name", "lead"])
    # Assert
    assert result.exit_code == 0 and fake.calls[0]["turn_url"] is None


# ---------------------------------------------------------------------------
# channel — cwd-walk self-peer discovery fallback (TG 12706, #356 follow-up)
# ---------------------------------------------------------------------------


class _RecordingChannelMain:
    """Plain-def recording callable (NOT MagicMock).

    Accepts ``name`` as positional OR keyword to match the
    CLI invocation shape, and records each call as a dict so the
    test asserts against the exact arguments forwarded.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, name=None, listen_url=None, turn_url=None) -> None:
        self.calls.append(
            {"name": name, "listen_url": listen_url, "turn_url": turn_url}
        )


def test_channel_runs_without_name_when_self_spec_present_in_cwd():
    # Arrange: drop a self spec under an isolated cwd and invoke `sac mcp
    # channel` with NO --name flag. The CLI should accept the missing
    # flag (optional now) and forward name=None down to channel.main —
    # discovery happens inside main, not in the CLI.
    fake = _RecordingChannelMain()
    runner = CliRunner()
    saved = mg._load_channel_main
    mg._load_channel_main = lambda: fake
    try:
        with runner.isolated_filesystem():
            from pathlib import Path

            spec = Path(".scitex/agent-container/agents/self/spec.yaml")
            spec.parent.mkdir(parents=True, exist_ok=True)
            spec.write_text("listen_url: http://127.0.0.1:7878\n")
            # Act
            result = runner.invoke(mcp, ["channel"])
    finally:
        mg._load_channel_main = saved
    # Assert: CLI accepted the missing flag and forwarded name=None.
    assert (
        result.exit_code == 0 and len(fake.calls) == 1 and fake.calls[0]["name"] is None
    )


# ---------------------------------------------------------------------------
# _enumerate_tools — version-agnostic shape detection (real classes only)
# ---------------------------------------------------------------------------


def test_enumerate_tools_returns_values_of_dict_attr_for_fastmcp_2x():
    # Arrange — FastMCP 2.x style: server.tools is a plain dict.
    class Srv:
        tools = {"x": _FakeTool("x"), "y": _FakeTool("y")}

    # Act
    result = mg._enumerate_tools(Srv())
    names = sorted(t.name for t in result)
    # Assert
    assert names == ["x", "y"]


def test_enumerate_tools_unwraps_tool_manager_inner_dict():
    # Arrange
    class Manager:
        _tools = {"q": _FakeTool("q")}

    class Srv:
        _tool_manager = Manager()

    # Act
    result = mg._enumerate_tools(Srv())
    # Assert
    assert [t.name for t in result] == ["q"]


def test_enumerate_tools_returns_empty_list_for_server_with_no_known_shape():
    # Arrange
    class Srv:
        pass

    # Act
    result = mg._enumerate_tools(Srv())
    # Assert
    assert result == []
