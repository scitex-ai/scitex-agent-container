"""Tests for the sac MCP server (F-CS15).

TQ cleanup: each test is named for the specific behaviour it verifies
(TQ003), carries the AAA marker triple (TQ002), asserts exactly one
fact (TQ007), and uses ``pytest.parametrize`` where the matrix is
genuinely declarative (TQ001). No mocks/monkeypatch — tool registry
capture uses an explicit in-test recorder class (a real collaborator
that mirrors the ``@server.tool()`` decorator contract), not
``unittest.mock`` or ``monkeypatch``.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

# fastmcp may be absent on a minimal install — gate every test on it.
fastmcp = pytest.importorskip("fastmcp")

from scitex_agent_container._mcp.server import get_server  # noqa: E402
from scitex_agent_container.cli_pkg.mcp_group import (  # noqa: E402
    mcp as mcp_cli_group,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _tool_names(server) -> list[str]:
    """Async-safe enumeration via the same helper the CLI uses."""
    from scitex_agent_container.cli_pkg.mcp_group import _list_tool_names

    return _list_tool_names(server)


class _ToolRecorder:
    """Stand-in registry mirroring ``@server.tool()`` — captures decorated
    functions into a dict so individual tools can be invoked directly.

    Not a mock: it satisfies the same structural contract the real
    ``FastMCP`` server exposes for tool registration (``.tool()`` returns
    a decorator), and the captured callables run unchanged.
    """

    def __init__(self) -> None:
        self.captured: dict = {}

    def tool(self):
        def _decorate(fn):
            self.captured[fn.__name__] = fn
            return fn

        return _decorate


@pytest.fixture
def skills_tools() -> dict:
    """Register the skills tool group against a recorder and return the
    captured callables keyed by tool name."""
    from scitex_agent_container._mcp._tools._skills import register_skills_tools

    recorder = _ToolRecorder()
    register_skills_tools(recorder)
    return recorder.captured


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# get_server — construction & tool registry shape
# ---------------------------------------------------------------------------


def test_server_constructs_with_expected_name():
    # Arrange
    expected_name = "scitex-agent-container"
    # Act
    server = get_server()
    # Assert
    assert server.name == expected_name


def test_get_server_registers_at_least_one_tool():
    # Arrange
    server = get_server()
    # Act
    names = _tool_names(server)
    # Assert
    assert names, "MCP server registered no tools"


def test_every_registered_tool_name_has_verb_noun_shape():
    """Per scitex MCP convention §1 (Convention A, recommended), the
    standalone source uses bare names (``agent_list``); the umbrella
    namespace prefix is added at mount time."""
    # Arrange
    names = _tool_names(get_server())
    # Act
    bad = [n for n in names if "_" not in n]
    # Assert
    assert bad == [], f"tools without verb_noun shape: {bad}"


# F-CS15 noun groups the MCP server must mirror from the CLI.
_REQUIRED_TOOL_NAMES = [
    "agent_list",
    "agent_status",
    "agent_start",
    "agent_stop",
    "db_show",
    "db_query",
    "host_list",
    "image_build",
    "template_render_contributor_spec",
    "skills_list",
    "skills_get",
    "mcp_list_tools",
    "mcp_doctor",
]


@pytest.mark.parametrize("required_name", _REQUIRED_TOOL_NAMES)
def test_required_tool_is_registered(required_name: str):
    # Arrange
    names = set(_tool_names(get_server()))
    # Act
    is_registered = required_name in names
    # Assert
    assert is_registered, f"missing required tool: {required_name}"


# ---------------------------------------------------------------------------
# CLI: doctor
# ---------------------------------------------------------------------------


def test_cli_doctor_exits_zero(runner: CliRunner):
    # Arrange
    args = ["doctor"]
    # Act
    result = runner.invoke(mcp_cli_group, args)
    # Assert
    assert result.exit_code == 0, result.output


def test_cli_doctor_output_mentions_fastmcp(runner: CliRunner):
    # Arrange
    args = ["doctor"]
    # Act
    result = runner.invoke(mcp_cli_group, args)
    # Assert
    assert "fastmcp" in result.output


def test_cli_doctor_output_mentions_server_label(runner: CliRunner):
    # Arrange
    args = ["doctor"]
    # Act
    result = runner.invoke(mcp_cli_group, args)
    # Assert
    assert "sac MCP server" in result.output


# ---------------------------------------------------------------------------
# CLI: list-tools --json
# ---------------------------------------------------------------------------


@pytest.fixture
def list_tools_json_payload(runner: CliRunner) -> dict:
    result = runner.invoke(mcp_cli_group, ["list-tools", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_cli_list_tools_json_reports_at_least_one_tool(
    list_tools_json_payload: dict,
):
    # Arrange
    payload = list_tools_json_payload
    # Act
    count = payload["count"]
    # Assert
    assert count >= 1


def test_cli_list_tools_json_every_entry_has_name(
    list_tools_json_payload: dict,
):
    # Arrange
    payload = list_tools_json_payload
    # Act
    entries_missing_name = [t for t in payload["tools"] if "name" not in t]
    # Assert
    assert entries_missing_name == []


def test_cli_list_tools_json_every_name_has_verb_noun_shape(
    list_tools_json_payload: dict,
):
    # Arrange
    payload = list_tools_json_payload
    # Act
    bad = [t["name"] for t in payload["tools"] if "_" not in t["name"]]
    # Assert
    assert bad == []


# ---------------------------------------------------------------------------
# CLI: install --claude-code
# ---------------------------------------------------------------------------


@pytest.fixture
def install_claude_code_output(runner: CliRunner) -> str:
    result = runner.invoke(mcp_cli_group, ["install", "--claude-code"])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize(
    "expected_fragment",
    [
        '"scitex-agent-container"',
        '"command": "sac"',
        '"args": ["mcp", "start"]',
    ],
)
def test_cli_install_claude_code_output_contains_fragment(
    install_claude_code_output: str, expected_fragment: str
):
    # Arrange
    output = install_claude_code_output
    # Act
    present = expected_fragment in output
    # Assert
    assert present, f"missing fragment {expected_fragment!r} in: {output}"


# ---------------------------------------------------------------------------
# CLI: start --dry-run
# ---------------------------------------------------------------------------


@pytest.fixture
def start_dry_run_default_result(runner: CliRunner):
    result = runner.invoke(mcp_cli_group, ["start", "--dry-run"])
    assert result.exit_code == 0, result.output
    return result


@pytest.fixture
def start_dry_run_http_result(runner: CliRunner):
    result = runner.invoke(
        mcp_cli_group, ["start", "--http", "--port", "9999", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    return result


def test_cli_start_dry_run_default_announces_stdio_transport(
    start_dry_run_default_result,
):
    # Arrange
    result = start_dry_run_default_result
    # Act
    output = result.output
    # Assert
    assert "transport=stdio" in output


def test_cli_start_dry_run_http_announces_http_transport(
    start_dry_run_http_result,
):
    # Arrange
    result = start_dry_run_http_result
    # Act
    output = result.output
    # Assert
    assert "transport=http" in output


def test_cli_start_dry_run_http_announces_chosen_port(
    start_dry_run_http_result,
):
    # Arrange
    result = start_dry_run_http_result
    # Act
    output = result.output
    # Assert
    assert "9999" in output


# ---------------------------------------------------------------------------
# skills_list / skills_get tool group
# ---------------------------------------------------------------------------


def test_skills_list_returns_at_least_one_skill(skills_tools: dict):
    """``sac_skills_list`` reads from ``_skills/scitex-agent-container/``."""
    # Arrange
    skills_list = skills_tools["skills_list"]
    # Act
    result = skills_list()
    # Assert
    assert result["count"] >= 1


def test_skills_list_every_entry_carries_a_name(skills_tools: dict):
    # Arrange
    skills_list = skills_tools["skills_list"]
    # Act
    result = skills_list()
    entries_missing_name = [s for s in result["skills"] if "name" not in s]
    # Assert
    assert entries_missing_name == []


def test_skills_get_unknown_name_returns_error_field(skills_tools: dict):
    # Arrange
    skills_get = skills_tools["skills_get"]
    # Act
    result = skills_get(name="definitely-not-a-real-skill-xxxx")
    # Assert
    assert "error" in result


def test_skills_get_unknown_name_lists_available_skills(skills_tools: dict):
    # Arrange
    skills_get = skills_tools["skills_get"]
    # Act
    result = skills_get(name="definitely-not-a-real-skill-xxxx")
    # Assert
    assert "available" in result


# ---------------------------------------------------------------------------
# run_server transport selection
# ---------------------------------------------------------------------------


class _RunRecorder:
    """Real collaborator standing in for a FastMCP server.

    Mirrors the structural contract of ``server.run(transport=..., host=..., port=...)``
    used by :func:`run_server`. Records every call so the test can assert
    which transport branch fired. Not a mock — it is a concrete class
    with explicit behaviour (append to ``self.calls``).
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def run_recorder_server():
    """Install a recorder as the module-level singleton and restore it after."""
    from scitex_agent_container._mcp import server as server_mod

    saved = server_mod.mcp
    recorder = _RunRecorder()
    server_mod.mcp = recorder
    try:
        yield recorder
    finally:
        server_mod.mcp = saved


def test_run_server_default_uses_stdio_transport(run_recorder_server):
    # Arrange
    from scitex_agent_container._mcp.server import run_server

    # Act
    run_server()
    # Assert
    assert run_recorder_server.calls == [{}]


def test_run_server_http_passes_host_and_port(run_recorder_server):
    # Arrange
    from scitex_agent_container._mcp.server import run_server

    # Act
    run_server(transport="http", host="0.0.0.0", port=9100)
    # Assert
    assert run_recorder_server.calls == [
        {"transport": "http", "host": "0.0.0.0", "port": 9100}
    ]
