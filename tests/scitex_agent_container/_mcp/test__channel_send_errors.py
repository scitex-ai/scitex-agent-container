"""The FAIL-LOUD contract: a send that reached nobody must FAIL the call.

Why this module exists as its own file
--------------------------------------
``test__channel_tools.py`` already asserted that a 0-subscriber send returns
a body containing an ``error`` key. Those tests passed — and agents silently
swallowed each other's messages anyway.

The gap: a caller does not decide "did my call succeed?" by reading the body.
It reads the MCP protocol's ``isError`` flag. And the low-level server stamps
``isError=False`` on ANY plain ``list[TextContent]`` a handler returns — so
"reached no live subscriber" was arriving inside a result the protocol
classified as SUCCESSFUL. The old tests could not have caught that, because
they invoked the handler directly and never went through the component that
produces the flag.

So every test here drives a REAL ``mcp.server.lowlevel.Server`` against a
REAL loopback HTTP listen. No mocks. The assertions are about what the caller
actually sees.

The three states (and why the middle one is the only failure)
-------------------------------------------------------------
* ``delivered_subscriber_count >= 1`` → delivered. Success.
* ``delivered_subscriber_count == 0``  → DEFINITIVELY not delivered. The bus
  fanned out to nobody. This is evidence, so it fails loudly.
* field ABSENT (e.g. a cross-host forward) → could not determine. Inventing a
  zero would be a false accusation of non-delivery, so it does NOT fail.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("mcp.types")  # gates the module on `mcp`

from scitex_agent_container._mcp._channel_send_errors import (  # noqa: E402
    ERR_NO_SUBSCRIBER,
    ERR_UNKNOWN_TARGET,
    ERR_UNREACHABLE,
    no_subscriber_error,
    suggest_names,
    unknown_target_error,
)
from scitex_agent_container._mcp._channel_tools import register_tools  # noqa: E402


# ---------------------------------------------------------------------------
# A real loopback listen. Speaks just enough HTTP/1.1 to answer message:send
# with a configurable publish reply — the same shape sac listen returns from
# ``node_message_send``.
# ---------------------------------------------------------------------------


class _FakeListen:
    """Real asyncio TCP server answering ``POST /agents/<n>/message:send``."""

    def __init__(self) -> None:
        self.send_response: dict[str, Any] = {"ok": True}
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if not await reader.readline():
                return
            content_length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            if content_length:
                await reader.readexactly(content_length)
            body = json.dumps(self.send_response).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


@pytest_asyncio.fixture
async def listen():
    server = _FakeListen()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _call(listen_url: str, tool: str, args: dict[str, Any]):
    """Invoke ``tool`` through a REAL MCP Server; return its CallToolResult.

    This is the caller's-eye view. The low-level server is what turns a
    handler's return value into the ``CallToolResult`` (and its ``isError``
    flag) that the calling model receives — so it is the only place the
    swallowed-message bug was ever observable.
    """
    import mcp.types as types
    from mcp.server.lowlevel import Server

    server = Server(name="sac-channel-test")
    register_tools(server, agent_name="alice", listen_url=listen_url, bearer=None)
    handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=tool, arguments=args),
    )
    return (await handler(request)).root


def _body(result) -> dict[str, Any]:
    return json.loads(result.content[0].text)


# A refusing port comes from the shared ``dead_port`` fixture
# (tests/scitex_agent_container/_helpers/ports.py, wired in tests/conftest.py):
# bound WITHOUT listening so a connect is refused, and HELD so nothing else can
# bind it mid-test. The helper that used to live here released the port first.


# ---------------------------------------------------------------------------
# THE BUG: 0 subscribers came back as a SUCCESSFUL tool call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_subscriber_send_is_an_mcp_error(listen):
    """delivered_subscriber_count == 0 → the caller must see a FAILED call,
    not a success whose body merely mentions an error."""
    # Arrange — the listen publishes to nobody.
    listen.send_response = {"msg_id": "m1", "delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert result.isError is True


@pytest.mark.asyncio
async def test_delivered_send_is_not_an_mcp_error(listen):
    """Guard against the fail-loud check over-firing: >= 1 subscriber is a
    real delivery and must stay a plain success."""
    # Arrange
    listen.send_response = {"msg_id": "m1", "delivered_subscriber_count": 1}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert result.isError is False


@pytest.mark.asyncio
async def test_absent_subscriber_count_is_not_an_mcp_error(listen):
    """Three states, not two. An ABSENT count (a cross-host forward that does
    not report one) is "could not determine" — it must NOT be inferred as zero
    and failed. Absence of evidence is not evidence of non-delivery."""
    # Arrange — a 200 carrying no delivered_subscriber_count at all.
    listen.send_response = {"ok": True}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert result.isError is False


@pytest.mark.asyncio
async def test_unreachable_listen_is_an_mcp_error(dead_port):
    """Connection refused is a demonstrable non-delivery — also a caller-
    visible failure, not a quietly-swallowed one."""
    # Arrange — nothing is listening on this port, and it is HELD so nothing
    # can start.
    url = dead_port.url("")
    # Act
    result = await _call(url, "a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert result.isError is True


@pytest.mark.asyncio
async def test_reply_to_unknown_msg_id_is_an_mcp_error(listen):
    """A reply that resolved no recipient delivered nothing either."""
    # Arrange — nothing in the inbox ring, so this msg_id resolves to no sender.
    unknown_msg_id = "ghost"
    # Act
    result = await _call(
        listen.base_url, "a2a_reply", {"in_reply_to": unknown_msg_id, "content": "x"}
    )
    # Assert
    assert result.isError is True


# ---------------------------------------------------------------------------
# The failure must stay ACTIONABLE — loud is not enough if it strands the
# caller. The detail (who, how many, what now) survives the fail-loud change.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_subscriber_error_names_the_target(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["target"] == "bob"


@pytest.mark.asyncio
async def test_no_subscriber_error_carries_machine_readable_code(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["code"] == ERR_NO_SUBSCRIBER


@pytest.mark.asyncio
async def test_no_subscriber_error_reports_the_subscriber_count(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["delivered_subscriber_count"] == 0


@pytest.mark.asyncio
async def test_no_subscriber_error_states_delivered_false(listen):
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["delivered"] is False


@pytest.mark.asyncio
async def test_no_subscriber_error_reports_the_message_as_durably_queued(listen):
    """sac listen persists to ``channel_events`` BEFORE it publishes, and
    replays undelivered rows on the target's next connect. "Not delivered"
    therefore does not mean "lost" — and telling the caller to re-send would
    double-deliver once the adapter reconnects."""
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    # Assert
    assert _body(result)["durably_queued"] is True


@pytest.mark.asyncio
async def test_no_subscriber_error_does_not_prescribe_a_restart(listen):
    """0 subscribers means a DETACHED INBOX ADAPTER, not a dead agent. The
    remedy must never be one that destroys a healthy session."""
    # Arrange
    listen.send_response = {"delivered_subscriber_count": 0}
    # Act
    result = await _call(
        listen.base_url, "a2a_send", {"target": "bob", "content": "hi"}
    )
    advice = " ".join(_body(result)["what_to_do"]).lower()
    # Assert
    assert "do not force-restart" in advice


@pytest.mark.asyncio
async def test_unreachable_error_carries_machine_readable_code(dead_port):
    # Arrange
    url = dead_port.url("")
    # Act
    result = await _call(url, "a2a_send", {"target": "bob", "content": "hi"})
    # Assert
    assert _body(result)["code"] == ERR_UNREACHABLE

_KNOWN = [
    "scitex-agent-container-04",
    "scitex-dev",
    "scitex-hub",
    "scitex-storage",
]


def test_unknown_target_carries_its_own_failure_code():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    code = err.code
    # Assert — a caller must branch on the CLASS, not string-match prose.
    assert code == ERR_UNKNOWN_TARGET


def test_unknown_target_is_not_the_no_subscriber_code():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    code = err.code
    # Assert — collapsing these two is the entire bug.
    assert code != ERR_NO_SUBSCRIBER


def test_unknown_target_does_not_claim_the_message_is_queued():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    queued = err.detail["durably_queued"]
    # Assert — the load-bearing field. Claiming True is what made a real
    # message wait forever.
    assert queued is False


def test_detached_adapter_still_claims_durable_queueing():
    # Arrange — the OTHER case must keep its existing promise.
    err = no_subscriber_error("scitex-dev")
    # Act
    queued = err.detail["durably_queued"]
    # Assert
    assert queued is True


def test_unknown_target_suggests_the_real_name():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    suggestions = err.detail["suggestions"]
    # Assert — a typo should cost seconds, not an indefinite wait.
    assert "scitex-agent-container-04" in suggestions


def test_unknown_target_names_the_real_name_in_the_message():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    text = str(err)
    # Assert — the human sentence must carry it too; not every caller
    # reads `detail`.
    assert "scitex-agent-container-04" in text


def test_unknown_target_tells_the_caller_to_re_send():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    advice = " ".join(err.detail["what_to_do"]).lower()
    # Assert — the exact inversion of the no_subscriber advice.
    assert "re-send" in advice


def test_detached_adapter_tells_the_caller_not_to_re_send():
    # Arrange
    err = no_subscriber_error("scitex-dev")
    # Act
    advice = " ".join(err.detail["what_to_do"]).lower()
    # Assert
    assert "do not re-send" in advice


def test_plain_difflib_would_have_missed_the_real_case():
    # Arrange — the reason suggest_names exists rather than a one-liner.
    import difflib

    # Act
    naive = difflib.get_close_matches("sac-04", _KNOWN, n=3, cutoff=0.4)
    # Assert — character similarity calls these unrelated strings. A future
    # refactor back to plain difflib silently reintroduces the miss, so pin
    # the inadequacy itself.
    assert "scitex-agent-container-04" not in naive


def test_acronym_of_a_registered_name_is_suggested():
    # Arrange — 'sac' is the initials of scitex-agent-container; this is the
    # house naming convention and the most likely way a name goes wrong.
    # Act
    suggestions = suggest_names("sac", _KNOWN)
    # Assert
    assert "scitex-agent-container-04" in suggestions


def test_a_shared_instance_suffix_is_suggested():
    # Arrange — right instance, wrong package name.
    # Act
    suggestions = suggest_names("wrongname-04", _KNOWN)
    # Assert
    assert "scitex-agent-container-04" in suggestions


def test_an_ordinary_typo_is_still_suggested():
    # Arrange — character similarity must keep working.
    # Act
    suggestions = suggest_names("scitex-hubb", _KNOWN)
    # Assert
    assert "scitex-hub" in suggestions


def test_an_unrelated_name_is_not_suggested():
    # Arrange — a suggester that matches everything is noise.
    # Act
    suggestions = suggest_names("zzzzzz", _KNOWN)
    # Assert
    assert suggestions == []


def test_a_name_resembling_nothing_yields_no_suggestions():
    # Arrange — nothing in the registry resembles this.
    err = unknown_target_error("zzzzzz", _KNOWN)
    # Act
    suggestions = err.detail["suggestions"]
    # Assert — a bad guess would be worse than none.
    assert suggestions == []


def test_a_name_resembling_nothing_still_points_at_the_registry():
    # Arrange
    err = unknown_target_error("zzzzzz", _KNOWN)
    # Act
    text = str(err)
    # Assert — with no suggestion to offer, say where to look instead.
    assert "a2a_peers" in text or "registered" in text


def test_an_empty_registry_yields_no_suggestions():
    # Arrange — the registry read came back with nothing at all.
    err = unknown_target_error("sac-04", [])
    # Act
    suggestions = err.detail["suggestions"]
    # Assert
    assert suggestions == []


def test_an_empty_registry_still_produces_a_message():
    # Arrange
    err = unknown_target_error("sac-04", [])
    # Act
    text = str(err)
    # Assert — must degrade to a sentence, not an exception.
    assert text


@pytest.mark.parametrize("code", [ERR_UNKNOWN_TARGET, ERR_NO_SUBSCRIBER])
def test_both_failure_modes_report_not_delivered(code):
    # Arrange
    err = (
        unknown_target_error("sac-04", _KNOWN)
        if code == ERR_UNKNOWN_TARGET
        else no_subscriber_error("scitex-dev")
    )
    # Act
    delivered = err.detail["delivered"]
    # Assert — they differ on recoverability, never on delivery.
    assert delivered is False


# ---------------------------------------------------------------------------
# The 2026-08-18 incident: the miss was fleet-wide UNKNOWN, but the old
# message read as fleet-wide ABSENCE — and a caller acted on it as a death
# verdict. The typo case (same host) is unchanged; these pin the SCOPE.
# ---------------------------------------------------------------------------


def test_unknown_target_says_the_verdict_is_host_local():
    # Arrange — a name registered on ANOTHER host, unregistered here.
    err = unknown_target_error("scitex-agent-container", _KNOWN)
    # Act
    text = str(err).lower()
    # Assert — the message must state the population it observed.
    assert "this host" in text
    assert "other hosts" in text


def test_unknown_target_does_not_assert_fleet_wide_absence():
    # Arrange
    err = unknown_target_error("scitex-agent-container", _KNOWN)
    # Act
    text = str(err)
    # Assert — "will ever attach" claimed a fact about all future time from
    # one host's registry. That is the sentence that cost a live agent its
    # work on 2026-08-18.
    assert "will ever attach" not in text
    assert "will never" not in text.lower()


def test_unknown_target_detail_carries_its_observation_scope():
    # Arrange
    err = unknown_target_error("sac-04", _KNOWN)
    # Act
    scope = err.detail["observation_scope"]
    # Assert — machine readers must not parse prose to learn what population
    # `registered: false` was measured against.
    assert scope == "host-local"


def test_unknown_target_advice_forbids_the_death_conclusion():
    # Arrange — the advice is where the ownership decision happened.
    err = unknown_target_error("scitex-agent-container", _KNOWN)
    # Act
    advice = " ".join(err.detail["what_to_do"]).lower()
    # Assert — a miss here is UNKNOWN fleet-wide: no death verdict, no
    # reassignment on its strength.
    assert "another host" in advice
    assert "dead" in advice
    assert "reassign" in advice
