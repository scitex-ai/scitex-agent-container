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
                pytest.fail(f"sac a2a serve exited early: {tail[-1000:]}")
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
    assert "result" in env, f"JSON-RPC error from server: {env}"
    task = env["result"]["task"]
    state = task["status"]["state"]
    assert "COMPLETED" in state, f"task state {state!r} (not completed); env={env}"
    return task["status"]["message"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# The core workflow assertion
# ---------------------------------------------------------------------------


def test_alpha_can_invoke_advertised_mcp_peers_tool(live_a2a_server: str) -> None:
    """The AgentCard lists `mcp__sac__a2a_peers` under
    `capabilities.extensions[].params.mcp_tools`. This test asserts the
    runtime ACTUALLY lets the model invoke it and that the response is
    the real peer listing — not a model hallucination, not a refusal.
    """
    reply = _send_text(
        live_a2a_server,
        "alpha",
        "Invoke the tool named mcp__sac__a2a_peers right now with no "
        "arguments. Quote its raw JSON result back to me verbatim, in "
        "a single fenced code block, with no other commentary. If the "
        "tool is not available in your tool list, reply with exactly: "
        "TOOL NOT REGISTERED",
    )
    # The card said the tool is there; the runtime must back that up.
    assert "TOOL NOT REGISTERED" not in reply, (
        "AgentCard advertises `mcp__sac__a2a_peers` under "
        "capabilities.extensions[], but the model says the tool isn't "
        "available. The card lied to the client. "
        f"Reply was:\n{reply}"
    )
    # The tool's actual output is `{"agents":[{"name":"alpha","url":...},{"name":"beta","url":...}]}`.
    # The model may add framing but the peer names should appear.
    assert "alpha" in reply and "beta" in reply, (
        "Tool may have been invoked but reply doesn't contain both peer "
        f"names. Reply was:\n{reply}"
    )


def test_alpha_can_message_beta_via_a2a_send(live_a2a_server: str) -> None:
    """The full agent-to-agent workflow: alpha invokes its
    ``mcp__sac__a2a_send`` tool to deliver a message to beta. Beta's
    inbox SSE must fire with ``from_agent: "alpha"`` and the right
    content. This is the canonical workflow the AgentCard advertises;
    if it fails, the system is broken end-to-end.
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

    assert sse_done.wait(timeout=15), (
        "alpha was asked to call mcp__sac__a2a_send to beta, but "
        "beta's inbox SSE never received an event. The tool was "
        "either not invoked or pointed at the wrong server."
    )
    ev = captured[0]
    assert ev.get("from_agent") == "alpha", (
        "beta got an event but from_agent isn't 'alpha'. The sidecar "
        "is supposed to auto-fill `from_agent` from its --name arg. "
        f"Got: {ev}"
    )
    assert "alpha" in (ev.get("content") or "").lower(), (
        f"beta's event content doesn't match what alpha was asked to "
        f"send. Got content={ev.get('content')!r}"
    )


def test_alpha_sees_advertised_mcp_send_tool(live_a2a_server: str) -> None:
    """`mcp__sac__a2a_send` is also advertised on the card; assert it
    appears in alpha's tool list when asked. We don't drive an actual
    send (that's a follow-up test) — just make sure the tool is at
    least visible to the model."""
    reply = _send_text(
        live_a2a_server,
        "alpha",
        "List the exact names of every tool you have access to. One "
        "tool name per line, no commentary. If you have no tools at "
        "all, reply with exactly: NO TOOLS.",
        timeout=60,
    )
    assert "NO TOOLS" not in reply, f"model reports no tools at all. Reply:\n{reply}"
    assert "mcp__sac__a2a_send" in reply, (
        "AgentCard advertises `mcp__sac__a2a_send` under "
        "capabilities.extensions[], but the model's tool list doesn't "
        f"include it. Reply was:\n{reply}"
    )


# EOF
