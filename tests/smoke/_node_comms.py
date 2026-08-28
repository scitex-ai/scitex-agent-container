"""Shared helpers for the node-comms smoke layer.

Both ``test_node_comms_e2e_http.py`` (raw HTTP/SSE transport) and
``test_node_comms_e2e_mcp.py`` (the ``a2a_*`` MCP tool layer) drive
the *same* substrate — a real ``sac listen`` on a loopback port and a
real ``state.db``. The wiring that brings that substrate up (lineage +
uvicorn + the A2A JSON-RPC body shape + SSE consumers) lives here so
neither test file duplicates it.

Every name in the cast used to get its OWN bearer, minted into the
``node_tokens`` table by ``mint_node_token``. That feature was removed
2026-08-28: nothing in ``src/`` ever minted a token, so the table was
empty on every fleet host and the per-node bearer the smoke layer
handed the server was a shape production never produced. The setup
functions still return a ``{name: bearer}`` map so the call sites are
unchanged — but every entry is now the HOST bearer, which is what a
real sac agent presents. Sender identity travels where it always
travelled in production: ``params.metadata.from_agent``.

Leading-underscore filename ⇒ pytest does not collect it as a test
module (``python_files = ["test_*.py"]``). Imported as
``from tests.smoke._node_comms import ...`` (the repo already imports
sibling helpers this way — see ``tests/e2e/test_agent_lifecycle.py``).

The shared **fixtures** (``disk_tmp``, ``comms_env``) live in
``conftest.py`` so pytest auto-discovers them; only plain functions
live here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path

import httpx
import yaml

from scitex_agent_container._state.state_db_acl_policy import record_comms_policy
from scitex_agent_container._state.state_db_nodes import record_lineage
from tests.scitex_agent_container._helpers.loopback_server import run_loopback

#: The one bearer the listen daemon admits. Every cast member presents
#: it (see the module docstring); ``create_app(token=tokens["host"])``
#: is what makes it valid.
HOST_TOKEN = "smoke-host-token"

# ---------------------------------------------------------------------------
# uvicorn loopback helpers (mirrors tests/.../_listen/test_server.py).
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind a loopback socket to port 0; return the OS-assigned port."""
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _run_loopback(app, port: int):
    """Spin uvicorn on ``127.0.0.1:port`` in a daemon thread.

    Teardown sets ``should_exit`` + joins; the ``finally`` fires even
    on test failure so a hung subscriber never strands the server.

    The startup wait lives in the shared helper — the hand-rolled 5s ceiling
    this used to carry raced the listen lifespan (measured 7.49s under load).
    See ``_helpers/loopback_server.py``.
    """
    with run_loopback(app, port) as p:
        yield p


# ---------------------------------------------------------------------------
# A2A JSON-RPC body shape — the same payload the SDK / MCP tool emits.
# ---------------------------------------------------------------------------


def _send_payload(text: str, *, from_agent: str | None) -> dict:
    msg: dict = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage"}
    message = {
        "message_id": "m-smoke",
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    params: dict = {"message": message}
    if from_agent is not None:
        params["metadata"] = {"from_agent": from_agent}
    msg["params"] = params
    return msg


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# SSE consumer helpers
# ---------------------------------------------------------------------------


async def _await_subscribed_and_read_one(
    url: str, *, headers: dict, ready: asyncio.Event, deadline_s: float = 5.0
) -> dict:
    """Open SSE, signal ``ready`` on the first comment frame, then
    return the first ``data:`` payload as JSON."""
    async with httpx.AsyncClient(timeout=deadline_s) as ac:
        async with ac.stream("GET", url, headers=headers) as sse:
            async for line in sse.aiter_lines():
                if line.startswith(":"):
                    ready.set()
                    continue
                if line.startswith("data:"):
                    return json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE {url!r} closed without a data frame")


async def _consume_event_with_id(url: str) -> tuple[str | None, dict]:
    """Open SSE (unauthenticated — used against a2a/_server.py), return
    ``(id_line_value, parsed_data)`` for the first event."""
    seen_id: str | None = None
    async with httpx.AsyncClient(timeout=5.0) as ac:
        async with ac.stream("GET", url) as sse:
            async for line in sse.aiter_lines():
                if line.startswith("id:"):
                    seen_id = line[len("id:") :].strip()
                    continue
                if line.startswith("data:"):
                    return seen_id, json.loads(line[len("data:") :].lstrip())
    raise AssertionError(f"SSE {url!r} closed without a data frame")


# ---------------------------------------------------------------------------
# Listen-server bring-up: lineage for a fixed cast.
# ---------------------------------------------------------------------------


def _host_bearers(*names: str) -> dict[str, str]:
    """``{name: HOST_TOKEN}`` for every name, plus the ``"host"`` key.

    The map shape survives from when each name carried its own minted
    bearer; the values no longer differ because the host token is the
    only credential the daemon accepts.
    """
    return {"host": HOST_TOKEN, **{n: HOST_TOKEN for n in names}}


def _set_up_two_groups(db: Path) -> dict[str, str]:
    """Group A = parent_a + {alpha, beta}. Group B = parent_b + {gamma}.

    Returns ``{name: bearer}`` — see :func:`_host_bearers`.
    """
    record_lineage(child="alpha", parent="parent_a", db_path=db)
    record_lineage(child="beta", parent="parent_a", db_path=db)
    record_lineage(child="gamma", parent="parent_b", db_path=db)
    return _host_bearers("parent_a", "alpha", "beta", "parent_b", "gamma")


def _set_up_denied_send(db: Path) -> dict[str, str]:
    """alpha + gamma are SIBLINGS under parent_a; gamma REFUSES inbound
    sibling sends (``spec.comms.inbound.siblings = deny``).

    The substrate for every denied-send assertion in the smoke layer.

    Why not simply "cross-group with no grant", which is what these tests
    used to rely on: messaging is now DEFAULT-ALLOW cross-group (operator
    2026-07-03) — collaboration is not a security boundary — so a
    cross-group send with no grant ALLOWS. The deny path therefore needs a
    deny that SURVIVES the default. ``check_send_acl`` evaluates two such
    overrides BEFORE the default allow, and they are NOT interchangeable:

    * an explicit BLOCK returns ``("block", ...)``, which 403s the sender
      but DELIBERATELY shows the receiver NOTHING (no denied_attempt push,
      no approval prompt) so the block flag itself cannot leak. Triggering
      the deny that way would silently gut the receiver-side assertions.
    * a per-spec RELATIONSHIP deny returns ``("deny", ...)`` — the verdict
      that fires the receiver-side ``denied_attempt`` notification and the
      approval prompt. That is the path these tests exist to pin, so that
      is the one we trigger.

    Returns ``{name: bearer}`` — see :func:`_host_bearers`.
    """
    record_lineage(child="alpha", parent="parent_a", db_path=db)
    record_lineage(child="gamma", parent="parent_a", db_path=db)
    record_comms_policy(name="gamma", inbound_siblings="deny")
    return _host_bearers("parent_a", "alpha", "gamma")


def _set_up_four_siblings(db: Path) -> dict[str, str]:
    """One parent + four siblings: alpha, beta, gamma, zeta (group A only).

    Distinct cast from ``_set_up_two_groups`` so the fan-out case does
    not conflict with gamma's group-B placement there.
    """
    for child in ("alpha", "beta", "gamma", "zeta"):
        record_lineage(child=child, parent="parent_a", db_path=db)
    return _host_bearers("parent_a", "alpha", "beta", "gamma", "zeta")


# ---------------------------------------------------------------------------
# a2a/_server.py echo-agent YAML (replay surface).
# ---------------------------------------------------------------------------


def _write_a2a_yaml(tmp: Path, name: str) -> Path:
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "metadata": {
            "name": name,
            "labels": {"capabilities": "echo", "role": "assistant", "team": "smoke"},
        },
        "spec": {"a2a": {"handler": "echo", "port": 8888}},
    }
    p = tmp / f"{name}.yaml"
    p.write_text(yaml.safe_dump(body))
    return p
