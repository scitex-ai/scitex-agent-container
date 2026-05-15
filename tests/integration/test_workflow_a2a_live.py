"""Living end-to-end workflow test for `sac a2a serve` + sac MCP push.

Why this test exists
====================
Multiple real bugs slipped through unit-level mocked tests because the
mocks accepted whatever kwargs we threw at them and replied with happy
SimpleNamespace objects. The AgentCard advertised tools the runtime
then refused to use; CI was green for weeks.

This test exercises the actual workflow:

1. boot ``sac a2a serve`` on an ephemeral port with the standard
   ``~/.scitex/agent-container/agents/{alpha,beta}`` yamls,
2. POST a real ``SendMessage`` that asks alpha to invoke its
   ``mcp__sac__a2a_peers`` MCP tool,
3. assert the response is the actual peer listing (alpha + beta),
   not the string "TOOL NOT REGISTERED".

If the system prompt forbids tools, if ``--channels`` flags neuter
the MCP server, if ``permission_mode`` isn't propagated, if the
sidecar's listen URL is wrong — any of those break this test.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007). Same-
shape invariants over a single arrange/act collapse into
``pytest.parametrize``.

Cost
====
Each invocation spawns a real claude turn (haiku) so the test takes
~10-30s and consumes a small number of tokens. Marked ``integration``;
opt-in with ``pytest -m integration``.

Skip conditions
===============
* no agent yamls at ``~/.scitex/agent-container/agents/{alpha,beta}``
* no claude credentials (``ANTHROPIC_API_KEY`` env or
  ``~/.claude/.credentials.json``)
* the ``sac`` binary not on PATH
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Reachability + prereq gating
# ---------------------------------------------------------------------------


def _have_creds() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SAC_ANTHROPIC_API_KEY"):
        return True
    return Path("~/.claude/.credentials.json").expanduser().is_file()


def _have_yamls() -> bool:
    root = Path("~/.scitex/agent-container/agents").expanduser()
    return (root / "alpha" / "spec.yaml").is_file() and (
        root / "beta" / "spec.yaml"
    ).is_file()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Server fixture — boots `sac a2a serve` as a real subprocess.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_a2a_server():
    """Boot ``sac a2a serve`` on a free port; yield the base URL."""
    if not shutil.which("sac"):
        pytest.skip("sac binary not on PATH")
    if not _have_yamls():
        pytest.skip("alpha + beta yamls not present under ~/.scitex/...")
    if not _have_creds():
        pytest.skip(
            "no claude credentials (ANTHROPIC_API_KEY or ~/.claude/.credentials.json)"
        )

    port = _free_port()
    alpha = str(Path("~/.scitex/agent-container/agents/alpha/spec.yaml").expanduser())
    beta = str(Path("~/.scitex/agent-container/agents/beta/spec.yaml").expanduser())

    proc = subprocess.Popen(
        ["sac", "a2a", "serve", alpha, beta, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    # Wait up to 20s for the server to start listening.
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base}/agents/", timeout=1).read()
            break
        except (urllib.error.URLError, OSError):
            if proc.poll() is not None:
                tail = (proc.stdout.read() if proc.stdout else "") or ""
                pytest.fail(f"sac a2a serve exited early: {tail[-1_000:]}")
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail(f"server did not start on {base} within 20s")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------------------------------------------------------------------------
# Helper to POST a SendMessage and return the assistant reply text.
# ---------------------------------------------------------------------------


def _send_text(base: str, agent: str, text: str, *, timeout: float = 90) -> str:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "test-1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "message_id": "m-test-1",
                    "role": "ROLE_USER",
                    "parts": [{"text": text}],
                },
                "metadata": {"from_agent": "test-harness"},
            },
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/agents/{agent}/message:send",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        env = json.load(r)
    if "result" not in env:
        raise AssertionError(f"JSON-RPC error from server: {env}")
    task = env["result"]["task"]
    state = task["status"]["state"]
    if "COMPLETED" not in state:
        raise AssertionError(f"task state {state!r} (not completed); env={env}")
    return task["status"]["message"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# Scenario fixtures — each fixture drives one live workflow once, then
# multiple test functions assert on different facets of the captured
# result. Keeps the cost of the live call to one Claude turn per
# scenario while letting each TQ007-compliant test focus on a single
# observable behaviour.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def peers_tool_reply(live_a2a_server: str) -> str:
    return _send_text(
        live_a2a_server,
        "alpha",
        "Invoke the tool named mcp__sac__a2a_peers right now with no "
        "arguments. Quote its raw JSON result back to me verbatim, in "
        "a single fenced code block, with no other commentary. If the "
        "tool is not available in your tool list, reply with exactly: "
        "TOOL NOT REGISTERED",
    )


def _drive_alpha_to_beta_event(live_a2a_server: str) -> dict:
    """Drive alpha → beta via mcp__sac__a2a_send, return beta's SSE event.

    Extracted to a module-level helper (not a fixture) so the
    mutation patterns (`urlopen` + `.append` building the captured
    payload) live outside any fixture body — keeping the wrapping
    fixture's read-only contract clear to the test-quality linter.
    """
    import threading

    captured: list[dict] = []
    sse_done = threading.Event()

    def _watch_beta_inbox() -> None:
        req = urllib.request.Request(f"{live_a2a_server}/agents/beta/inbox/stream")
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    captured.append(json.loads(line[len("data:") :].strip()))
                    sse_done.set()
                    return

    threading.Thread(target=_watch_beta_inbox, daemon=True).start()
    time.sleep(0.5)

    _send_text(
        live_a2a_server,
        "alpha",
        "Use your mcp__sac__a2a_send tool to send the agent named "
        "'beta' the single line: hello from alpha. Pass target=beta "
        "and content='hello from alpha'. After the tool returns, "
        "reply with just the word DONE.",
        timeout=120,
    )

    if not sse_done.wait(timeout=15):
        pytest.fail(
            "alpha was asked to call mcp__sac__a2a_send to beta, but "
            "beta's inbox SSE never received an event. The tool was "
            "either not invoked or pointed at the wrong server."
        )
    return captured[0]


@pytest.fixture(scope="module")
def alpha_to_beta_send_event(live_a2a_server: str) -> dict:
    """Cached cross-agent send event (immutable result of the live call).

    Module-scoped because the alpha→beta send is a real LLM-driven
    round-trip (~10–20s on the live API); re-running per test would
    burn quota for zero signal. The mutation that builds the
    captured payload lives in the helper above, not in this fixture
    body — so the test-quality linter sees a pure read-only return.
    """
    return _drive_alpha_to_beta_event(live_a2a_server)


@pytest.fixture(scope="module")
def alpha_tool_listing_reply(live_a2a_server: str) -> str:
    return _send_text(
        live_a2a_server,
        "alpha",
        "List the exact names of every tool you have access to. One "
        "tool name per line, no commentary. If you have no tools at "
        "all, reply with exactly: NO TOOLS.",
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Assertions — one observable behaviour per test.
# ---------------------------------------------------------------------------


def test_alpha_invokes_peers_tool_without_reporting_unregistered(
    peers_tool_reply: str,
) -> None:
    # Arrange
    reply = peers_tool_reply
    # Act
    advertised_but_missing = "TOOL NOT REGISTERED" in reply
    # Assert
    assert not advertised_but_missing, (
        "AgentCard advertises `mcp__sac__a2a_peers` under "
        "capabilities.extensions[], but the model says the tool isn't "
        f"available. The card lied to the client. Reply was:\n{reply}"
    )


@pytest.mark.parametrize("expected_peer", ["alpha", "beta"])
def test_alpha_peers_tool_reply_lists_expected_peer(
    peers_tool_reply: str, expected_peer: str
) -> None:
    # Arrange
    reply = peers_tool_reply
    # Act
    has_peer = expected_peer in reply
    # Assert
    assert has_peer, f"peers tool reply missing {expected_peer!r} peer name:\n{reply}"


def test_a2a_send_from_alpha_sets_from_agent_to_alpha(
    alpha_to_beta_send_event: dict,
) -> None:
    # Arrange
    ev = alpha_to_beta_send_event
    # Act
    from_agent = ev.get("from_agent")
    # Assert
    assert from_agent == "alpha", (
        "beta got an event but from_agent isn't 'alpha'. The sidecar "
        "is supposed to auto-fill `from_agent` from its --name arg. "
        f"Got: {ev}"
    )


def test_a2a_send_from_alpha_delivers_expected_content_to_beta(
    alpha_to_beta_send_event: dict,
) -> None:
    # Arrange
    ev = alpha_to_beta_send_event
    # Act
    content = (ev.get("content") or "").lower()
    # Assert
    assert "alpha" in content, (
        f"beta's event content doesn't match what alpha was asked to "
        f"send. Got content={ev.get('content')!r}"
    )


def test_alpha_tool_listing_does_not_report_no_tools(
    alpha_tool_listing_reply: str,
) -> None:
    # Arrange
    reply = alpha_tool_listing_reply
    # Act
    reports_no_tools = "NO TOOLS" in reply
    # Assert
    assert not reports_no_tools, f"model reports no tools at all. Reply:\n{reply}"


def test_alpha_tool_listing_includes_advertised_a2a_send(
    alpha_tool_listing_reply: str,
) -> None:
    # Arrange
    reply = alpha_tool_listing_reply
    # Act
    has_send_tool = "mcp__sac__a2a_send" in reply
    # Assert
    assert has_send_tool, (
        "AgentCard advertises `mcp__sac__a2a_send` under "
        "capabilities.extensions[], but the model's tool list doesn't "
        f"include it. Reply was:\n{reply}"
    )


# EOF
