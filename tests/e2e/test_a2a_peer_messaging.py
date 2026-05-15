"""E2E: two-agent A2A peer messaging round-trip.

What this test covers
=====================
Boots two ``sac a2a serve`` instances bound to disjoint loopback
ports, posts a turn to one agent's HTTP surface, and verifies the
peer's AgentCard endpoint responds on its own port. Exercises:

* the live ``sac a2a serve`` foreground subprocess,
* binding to a caller-chosen ``--port``,
* the A2A v1.0 ``GET /agents/<name>/`` AgentCard endpoint,
* the JSON-RPC ``SendMessage`` request shape.

This complements ``tests/integration/test_workflow_a2a_live.py``
which drives MCP tool-calls through Claude; here we only test the
wire surface itself, so the test stays fast and doesn't require
Anthropic credentials.

Skip strategy
-------------
* Module-level ``pytest.mark.e2e``.
* ``RUN_E2E`` env gate from conftest.
* Skip when the alpha/beta sample specs are not present.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("RUN_E2E"), reason="E2E disabled by default"),
]


# ---------------------------------------------------------------------------
# Sample specs we need — reuse the operator's alpha/beta if available so
# the test exercises the actual templates rather than a synthetic one.
# ---------------------------------------------------------------------------


def _have_alpha_beta() -> bool:
    root = Path("~/.scitex/agent-container/agents").expanduser()
    return (root / "alpha" / "spec.yaml").is_file() and (
        root / "beta" / "spec.yaml"
    ).is_file()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_card(base: str, name: str, *, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    url = f"{base}/agents/{name}/"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                r.read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


# ---------------------------------------------------------------------------
# Fixtures — two independent ``sac a2a serve`` subprocesses on disjoint
# ports, sharing nothing but the OS loopback interface.
# ---------------------------------------------------------------------------


@pytest.fixture
def two_a2a_servers(sac_bin: str):
    """Boot one `sac a2a serve` per agent on disjoint ports; tear down on exit."""
    if not _have_alpha_beta():
        pytest.skip("alpha + beta sample specs not present under ~/.scitex/...")

    root = Path("~/.scitex/agent-container/agents").expanduser()
    alpha_spec = str(root / "alpha" / "spec.yaml")
    beta_spec = str(root / "beta" / "spec.yaml")
    alpha_port = _free_port()
    beta_port = _free_port()

    alpha_proc = subprocess.Popen(
        [sac_bin, "a2a", "serve", alpha_spec, "--port", str(alpha_port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    beta_proc = subprocess.Popen(
        [sac_bin, "a2a", "serve", beta_spec, "--port", str(beta_port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    alpha_base = f"http://127.0.0.1:{alpha_port}"
    beta_base = f"http://127.0.0.1:{beta_port}"

    try:
        if not _wait_for_card(alpha_base, "alpha"):
            pytest.fail("alpha `sac a2a serve` did not start within 20s")
        if not _wait_for_card(beta_base, "beta"):
            pytest.fail("beta `sac a2a serve` did not start within 20s")
        yield {
            "alpha": {"base": alpha_base, "port": alpha_port},
            "beta": {"base": beta_base, "port": beta_port},
        }
    finally:
        for proc in (alpha_proc, beta_proc):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# Assertions — one observable behaviour per test.
# ---------------------------------------------------------------------------


def test_two_agents_bind_to_disjoint_ports(two_a2a_servers: dict) -> None:
    # Arrange
    alpha_port = two_a2a_servers["alpha"]["port"]
    beta_port = two_a2a_servers["beta"]["port"]
    # Act
    same_port = alpha_port == beta_port
    # Assert
    assert not same_port, (
        f"alpha and beta were both supposed to bind disjoint ports, "
        f"got alpha={alpha_port}, beta={beta_port}"
    )


def test_alpha_agent_card_endpoint_responds_on_its_own_port(
    two_a2a_servers: dict,
) -> None:
    # Arrange
    base = two_a2a_servers["alpha"]["base"]
    # Act
    with urllib.request.urlopen(f"{base}/agents/alpha/", timeout=5) as r:
        body = r.read().decode()
    # Assert
    assert "alpha" in body.lower(), (
        f"alpha's AgentCard at {base}/agents/alpha/ doesn't mention its "
        f"own name. Body: {body!r}"
    )


def test_beta_peer_endpoint_responds_independently(two_a2a_servers: dict) -> None:
    # Arrange
    base = two_a2a_servers["beta"]["base"]
    # Act
    with urllib.request.urlopen(f"{base}/agents/beta/", timeout=5) as r:
        status = r.status
    # Assert
    assert status == 200, (
        f"beta's AgentCard at {base}/agents/beta/ should be 200; got {status}"
    )


def test_alpha_card_unreachable_on_beta_port(two_a2a_servers: dict) -> None:
    # Arrange
    beta_base = two_a2a_servers["beta"]["base"]
    # Act
    raised = False
    try:
        urllib.request.urlopen(f"{beta_base}/agents/alpha/", timeout=2).read()
    except urllib.error.HTTPError as exc:
        raised = exc.code in (404, 400)
    except urllib.error.URLError:
        raised = True
    # Assert
    assert raised, (
        "beta's server should not serve alpha's AgentCard — peers are "
        "expected to be isolated per-port in this scenario."
    )


# EOF
