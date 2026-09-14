"""Tests for the TUI A2A turn bridge (``runtimes._tui_turn_bridge``).

The bridge gives a ``runtime: tui`` agent the same ``/v1/turn`` endpoint
the SDK runner serves, so a bus-pushed message (the ``sac mcp channel``
subscriber's wake POST) DRIVES a turn in the idle TUI instead of timing
out on a dead port.

Real seams only (PA-306 no-mocks): the HTTP routes run against a REAL
``ThreadingHTTPServer`` on an ephemeral port, exercised by a REAL urllib
client; a recording ``on_turn`` callable stands in for the tmux inject.
The launcher is driven through a real ``spawn`` recorder (a plain
function), not a mock.

STX-TQ002 AAA markers (each on its own line) + STX-TQ007 one observable
assert + STX-TQ003 descriptive names.
"""

from __future__ import annotations

import errno
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator

import pytest

from scitex_agent_container.config._a2a_defaults import DEFAULT_A2A_HOST
from scitex_agent_container.runtimes import _tui_turn_bridge as bridge
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

# The STOP-path survivor sweep resolves the holder PID via lsof/ss/fuser; skip
# the real-survivor test on a bare host that ships none of them (the finder's
# parsing/fallback is covered by ``tests/.../_listen/test__port_holder.py``).
_HAS_PORT_DISCOVERY = bool(
    shutil.which("lsof") or shutil.which("ss") or shutil.which("fuser")
)


def _reserve_a2a_port() -> tuple[int, socket.socket | None]:
    """An a2a port that is free AND STAYS free — held, not merely observed.

    The previous form bound ``("127.0.0.1", 0)``, read ``getsockname()``, and
    CLOSED the socket, storing the bare int for the whole 5-7 minute session.
    Its docstring defended the close by arguing a later rebind would succeed
    (SO_REUSEADDR over TIME_WAIT). That answers "can I rebind it?" The tests
    depend on "will it still be free when I get there?", and a closed port
    returns to the kernel's ephemeral pool at once. It was a memory of a fact,
    not a reservation.

    MEASURED on Linux 6.8 (ephemeral range 32768-60999):
        released the old way -> re-issued to another process after 4,542 draws
        held as below        -> 0 re-issues in 120,000 draws
    So the race is real and the hold removes it. Two earlier attempts to
    reproduce it returned nulls (0 in 4,000 binds; 0/25 at a 10s delay) and
    both instruments were wrong in the same way: holding thousands of sockets
    SIMULTANEOUSLY consumes the pool instead of cycling the kernel's rotating
    allocation cursor, which is what re-issues a released port.

    Keeping the socket bound but NEVER ``listen()``-ed is transparent to
    ``port_is_free`` — that probe is the same SO_REUSEADDR bind — so the gate
    the tests exercise still passes, while a real rogue LISTENER still trips
    it. The tests keep their teeth.

    THE RE-CHECK IS NOT DEFENSIVE CLUTTER. Duplicate-bind-while-not-listening
    is LINUX semantics; on macOS/BSD a held socket would make ``port_is_free``
    read False and would fail every ``start_turn_bridge`` test
    DETERMINISTICALLY. So the helper asks the real probe whether its own hold
    is transparent, and releases if it is not, degrading to exactly today's
    behaviour. This fix can never turn a rare Linux flake into a certain
    non-Linux failure.

    This is the THIRD form of this constant. It was the literal 19007 — inside
    the live a2a range and genuinely held by the ``figrecipe`` agent on the
    self-hosted runner — which stalled PR #1117 twice. Commit 811c6a99 traded
    that DETERMINISTIC collision for a PROBABILISTIC one, and the coin came up
    tails three times in seven days (ports 35045 / 34121 / 59935), blocking
    two more unrelated PRs.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    if bridge.port_is_free("127.0.0.1", port):
        return port, probe
    probe.close()
    return port, None


# A RESERVED a2a port + a fake PID for the bridge tests
# (PEP 515 separators satisfy STX-NL001).
_PORT, _PORT_RESERVATION = _reserve_a2a_port()
_PID = 4_242


@pytest.mark.skipif(
    _PORT_RESERVATION is None,
    reason=(
        "this platform does not permit a hold that is transparent to "
        "port_is_free, so the module fell back to the draw-and-release form"
    ),
)
def test_the_reserved_port_still_reads_free_to_the_bridges_own_probe() -> None:
    """The reservation must not break the gate it exists to stabilise."""
    # Arrange
    host = "127.0.0.1"
    # Act
    observed = bridge.port_is_free(host, _PORT)
    # Assert
    assert observed is True, (
        "the session reservation made the module port look BUSY to the "
        "bridge's own probe, which would fail every start_turn_bridge test "
        "deterministically instead of rarely"
    )


@pytest.mark.skipif(
    _PORT_RESERVATION is None,
    reason=(
        "this platform does not permit a hold that is transparent to "
        "port_is_free, so the module fell back to the draw-and-release form"
    ),
)
def test_the_module_port_is_reserved_not_merely_observed_free() -> None:
    """The kernel must genuinely HOLD it, which is what excludes it.

    A plain socket -- no SO_REUSEADDR -- is the honest question "is this port
    occupied?". An occupied port is excluded from the ephemeral autobind pool,
    and that exclusion is the only thing stopping a co-tenant process being
    handed this port minutes after the module was imported.

    FAILS BEFORE THE FIX: the old helper closed its socket, so the port was
    genuinely unoccupied and this plain bind SUCCEEDED.
    """
    # Arrange
    plain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Act
    try:
        plain.bind(("127.0.0.1", _PORT))
        observed = None
    except OSError as exc:
        observed = exc.errno
    finally:
        plain.close()
    # Assert
    assert observed == errno.EADDRINUSE, (
        "the module port is not actually held, so the kernel may re-issue it "
        "to another process mid-session -- the exact race this reservation "
        f"exists to remove (plain bind returned {observed!r})"
    )


def _gate_says_free(_host: str, _port: int) -> bool:
    """The port gate's answer, supplied by the test instead of by the machine.

    ``start_turn_bridge`` takes ``port_free_fn`` as a first-class parameter
    precisely so a caller can decide this, and the busy-path tests below
    already inject the ``False`` half (``port_free_fn=lambda _h, _p: False``).
    This is the symmetric ``True``: dependency injection through a declared
    seam, not a mock, so PA-306 is satisfied.

    WHY THE TESTS BELOW MUST NOT ASK THE MACHINE. ``_free_a2a_port`` picks a
    port by binding 0 and reading the assignment back, then CLOSES the socket
    at module import. Everything after that is a gap: on a self-hosted runner
    with N xdist workers, several CI legs and real agents all churning the
    ephemeral range, something else can take that port before the test bind
    probe runs. Measured 2026-08-26, worker ``[gw5]``, port 59935 -- two
    ``start_turn_bridge`` tests red on ``pytest-matrix-on-ubuntu-py3.11``.

    That red was decided by SCHEDULING, not by the change under test. Two PRs
    with disjoint diffs -- #1222 (``cli_pkg/_dev_jobs_backend.py``) and #1224
    (``.github/ci/run-in-sif.sh``), neither touching ``runtimes/`` -- both
    failed the same leg with the same error, which is what a defect neither of
    them caused looks like.

    This is the SECOND round of the same defect. The port used to be the
    literal 19007, inside the live a2a range and genuinely held by the
    ``figrecipe`` agent on the self-hosted runner; that stalled PR #1117
    twice. Moving to an OS-assigned port turned a DETERMINISTIC collision into
    a PROBABILISTIC one -- better, but still a coin flip on a busy machine.
    Injecting the predicate removes the machine from the question entirely.

    The gate itself stays covered, and better than before: the busy path is
    asserted by injecting ``False``, and ``port_is_free``'s own real-socket
    behaviour is tested where it belongs, against a socket the test holds open.
    """
    return True


# ---------------------------------------------------------------------------
# is_turn_route
# ---------------------------------------------------------------------------
def test_is_turn_route_accepts_bare_v1_turn() -> None:
    # Arrange
    path = "/v1/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_accepts_named_turn_for_this_agent() -> None:
    # Arrange
    path = "/agents/figrecipe/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_accepts_the_a2a_message_send_verb() -> None:
    """The spelling every a2a caller in this package actually posts.

    Regression for a live cross-host outage (2026-09-02): peer sends to
    `figrecipe` on compute-03 all died with ``no turn route
    '/agents/figrecipe/message:send'`` while the agent was healthy. A peer
    that resolves the target to the host's listen port gets ``sac listen``,
    which serves this verb; one that resolves to the agent's own a2a port
    gets this bridge, which did not. The caller does not choose which, so
    both must answer it.
    """
    # Arrange
    path = "/agents/figrecipe/message:send"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is True


def test_is_turn_route_rejects_message_send_for_another_agent() -> None:
    """The misroute guard must survive the new alias.

    Widening the accepted set is exactly where a cross-agent leak gets
    introduced: a POST naming a DIFFERENT agent must still 404 rather than
    land in this session.
    """
    # Arrange
    path = "/agents/someone-else/message:send"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is False


def test_is_turn_route_rejects_named_route_for_other_agent() -> None:
    # Arrange
    path = "/agents/someone-else/turn"
    # Act
    accepted = bridge.is_turn_route(path, "figrecipe")
    # Assert
    assert accepted is False


# ---------------------------------------------------------------------------
# resolved_a2a_port
# ---------------------------------------------------------------------------
def test_resolved_a2a_port_returns_int_when_resolved() -> None:
    # Arrange
    config = SimpleNamespace(a2a=SimpleNamespace(port=_PORT))
    # Act
    port = bridge.resolved_a2a_port(config)
    # Assert
    assert port == _PORT


def test_resolved_a2a_port_none_when_port_is_auto_string() -> None:
    # Arrange
    config = SimpleNamespace(a2a=SimpleNamespace(port="auto"))
    # Act
    port = bridge.resolved_a2a_port(config)
    # Assert
    assert port is None


# ---------------------------------------------------------------------------
# HTTP server — real ThreadingHTTPServer on an ephemeral port
# ---------------------------------------------------------------------------
@pytest.fixture
def bridge_factory() -> Iterator[Callable[..., int]]:
    """Start real bridge servers on ephemeral ports; tear them all down."""
    servers = []
    threads = []
    exchanges: dict[str, dict] = {}
    sequence = {"value": 0}

    def exchange_open(
        *,
        agent: str,
        probe_url: str,
        exchange_id: str | None = None,
        delivery_id: str | None = None,
        initiator: str | None = None,
    ) -> tuple[str, str]:
        existing_id = next(
            (
                key
                for key, value in exchanges.items()
                if delivery_id and value.get("delivery_id") == delivery_id
            ),
            None,
        )
        if exchange_id is None and existing_id is None:
            sequence["value"] += 1
        exchange_id = (
            exchange_id
            or existing_id
            or (f"xch_20260912T000000Z_test-{sequence['value']}_abcdef")
        )
        opened_at = "2026-09-12T00:00:00+00:00"
        exchanges.setdefault(
            exchange_id,
            {
                "kind": "http",
                "code": 202,
                "message": f"accepted; poll `{probe_url}/{exchange_id}`",
                "final": False,
                "delivery_id": delivery_id,
                "initiator": initiator,
            },
        )
        return exchange_id, opened_at

    def exchange_finish(exchange_id: str, **kwargs: object) -> None:
        status = kwargs["status"]
        exchanges[exchange_id] = {
            **exchanges.get(exchange_id, {}),
            "kind": status.kind,
            "code": status.code,
            "message": status.message,
            "final": status.final,
        }

    def exchange_read(exchange_id: str) -> dict | None:
        return exchanges.get(exchange_id)

    def start(
        on_turn: Callable[..., None],
        agent_name: str = "figrecipe",
        *,
        on_control: Callable[[str], None] | None = None,
        open_exchange: Callable[..., tuple[str, str]] | None = None,
        read_exchange: Callable[[str], dict | None] | None = None,
    ) -> int:
        server = bridge.build_server(
            host="127.0.0.1",
            port=0,
            on_turn=on_turn,
            agent_name=agent_name,
            on_control=on_control,
            exchange_open=open_exchange or exchange_open,
            exchange_finish=exchange_finish,
            exchange_read=read_exchange or exchange_read,
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
        return int(port)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=5)


def _post(port: int, path: str, body: dict | None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5
        ) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _wait_exchange(port: int, exchange_id: str) -> dict:
    for _ in range(100):
        _status, body = _get(port, f"/v1/exchanges/{exchange_id}")
        if body["status_code"]["code"] != 202:
            return body
        time.sleep(0.001)
    return body


def test_post_v1_turn_delivers_text_to_on_turn(bridge_factory) -> None:
    # Arrange
    received: list[str] = []
    port = bridge_factory(lambda text, **_kw: received.append(text))
    # Act
    _status, body = _post(port, "/v1/turn", {"text": "hello fleet"})
    _wait_exchange(port, body["exchange_id"])
    # Assert
    assert received == ["hello fleet"]


def test_post_ordinary_bare_turn_keeps_asynchronous_accepted_contract(
    bridge_factory,
) -> None:
    # Arrange
    receipt = SimpleNamespace(status="steered", visibility="session.queue[0]")
    port = bridge_factory(lambda text, **_kw: receipt)
    # Act
    status, body = _post(port, "/v1/turn", {"text": "hi there"})
    # Assert
    assert (status, body["status_code"]["kind"], body["status_code"]["code"]) == (
        202,
        "http",
        202,
    )


def test_post_ordinary_callback_none_concludes_exchange_successfully(
    bridge_factory,
) -> None:
    # Arrange — the real legacy callback returns None after successful inject.
    port = bridge_factory(lambda _text, **_kw: None)
    # Act
    status, body = _post(port, "/v1/turn", {"text": "hi there"})
    final = _wait_exchange(port, body["exchange_id"])
    # Assert
    assert (status, final["status_code"]["code"]) == (202, 200)


def test_production_callback_requires_visibility_for_ordinary_sac_turn(
    bridge_factory,
) -> None:
    # Arrange — no caller-supplied delivery id, matching `sac agents send`.
    observed: dict[str, object] = {}

    def production_visible(text: str, **kwargs: object) -> object:
        observed["text"] = text
        observed.update(kwargs)
        return SimpleNamespace(
            status="steered",
            visibility="session.inflight.corrections",
            delivery_mode="steer",
        )

    production_visible._sac_requires_visible_delivery = True  # type: ignore[attr-defined]
    port = bridge_factory(production_visible, agent_name="scitex-cards")

    # Act
    status, body = _post(port, "/v1/turn", {"text": "inspect the inbox"})
    final = _wait_exchange(port, body["exchange_id"])

    # Assert
    exchange_id = body["exchange_id"]
    assert (
        status,
        final["status_code"]["code"],
        observed["visible_delivery_id"],
        "proof=session.inflight.corrections" in final["status_code"]["message"],
    ) == (202, 200, exchange_id, True)


def test_production_callback_never_finalizes_without_visibility_proof(
    bridge_factory,
) -> None:
    # Arrange — Hermes accepted an RPC but returned no transcript proof.
    def accepted_without_visibility(_text: str, **_kwargs: object) -> None:
        return None

    accepted_without_visibility._sac_requires_visible_delivery = True  # type: ignore[attr-defined]
    port = bridge_factory(accepted_without_visibility, agent_name="scitex-cards")

    # Act
    status, body = _post(port, "/v1/turn", {"text": "inspect the inbox"})
    final = _wait_exchange(port, body["exchange_id"])

    # Assert
    assert (
        status,
        final["status_code"]["code"],
        final["receipt"]["state"],
        final["receipt"]["final"],
    ) == (202, 102, "retryable", False)


def test_claude_tui_marked_turn_concludes_without_hermes_receipt(
    bridge_factory,
) -> None:
    # Arrange — Claude Code's pane adapter returns bool, which _build_on_turn
    # normalizes to None. A durable A2A marker does not change its harness.
    submitted: list[tuple[str, bool]] = []

    def send_turn(_config, text: str, *, wait_ready: bool) -> bool:
        submitted.append((text, wait_ready))
        return True

    callback = bridge._build_on_turn(
        SimpleNamespace(name="scitex-hub"),
        runtime=SimpleNamespace(send_turn=send_turn),
    )
    port = bridge_factory(callback, agent_name="scitex-hub")
    marker = "fb16eec5dc784aa38b4287869daf5bf9"
    text = f"A2A bridge verification only.\n<!-- delivery:{marker} -->"

    # Act
    status, body = _post(
        port,
        "/v1/turn",
        {"text": text, "visible_delivery_id": marker},
    )
    final = _wait_exchange(port, body["exchange_id"])

    # Assert
    assert (
        status,
        final["status_code"]["code"],
        final["status_code"]["message"],
        submitted,
    ) == (
        202,
        200,
        "the target TUI accepted the identified turn",
        [(text, False)],
    )


def test_post_refuses_acceptance_when_canonical_ledger_is_unavailable(
    bridge_factory,
) -> None:
    # Arrange: the exception deliberately contains credential-shaped material;
    # HTTP diagnostics must never echo it.
    def unavailable(**_kwargs: object) -> tuple[str, str]:
        raise RuntimeError("postgres://operator:secret@example.invalid/db")

    port = bridge_factory(
        lambda text, **_kw: None,
        open_exchange=unavailable,
    )

    # Act
    status, body = _post(port, "/v1/turn", {"text": "hi there"})

    # Assert
    assert (
        status,
        body["status_code"]["code"],
        "secret" not in json.dumps(body),
    ) == (503, 503, True)


def test_post_v1_control_delivers_enter_without_a_pid_file(bridge_factory) -> None:
    # Arrange
    received: list[str] = []
    port = bridge_factory(lambda _text, **_kw: None, on_control=received.append)
    # Act
    status, body = _post(
        port, "/v1/control", {"kind": "control", "action": "ui.key", "key": "Enter"}
    )
    # Assert
    assert (status, body.get("mode"), received) == (200, "tui-control", ["Enter"])


def test_post_v1_control_rejects_unbounded_keys(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda _text, **_kw: None, on_control=lambda _key: None)
    # Act
    status, body = _post(port, "/v1/control", {"key": "F12"})
    # Assert
    assert status == 400 and "Enter, Escape, or C-c" in body["error"]


def test_post_named_turn_route_delivers_for_this_agent(bridge_factory) -> None:
    # Arrange
    received: list[str] = []
    port = bridge_factory(
        lambda text, **_kw: received.append(text), agent_name="figrecipe"
    )
    # Act
    _status, body = _post(port, "/agents/figrecipe/turn", {"text": "named route"})
    _wait_exchange(port, body["exchange_id"])
    # Assert
    assert received == ["named route"]


def test_post_threads_requester_identity_to_on_turn(bridge_factory) -> None:
    # Arrange — capture the requester kwargs the handler forwards.
    seen: dict = {}

    def rec(
        text: str,
        *,
        from_agent=None,
        dispatch_id=None,
        visible_delivery_id=None,
        delivery_mode=None,
    ) -> object:
        seen["from_agent"] = from_agent
        seen["dispatch_id"] = dispatch_id
        seen["visible_delivery_id"] = visible_delivery_id
        seen["delivery_mode"] = delivery_mode
        return SimpleNamespace(status="steered", visibility="session.queue[0]")

    port = bridge_factory(rec)
    # Act
    _status, body = _post(
        port,
        "/v1/turn",
        {"text": "hi", "from_agent": "lead", "dispatch_id": "d1"},
    )
    _wait_exchange(port, body["exchange_id"])
    # Assert
    assert (
        seen["from_agent"],
        seen["dispatch_id"],
        seen["visible_delivery_id"],
        seen["delivery_mode"],
    ) == (
        "lead",
        "d1",
        None,
        "steer",
    )


def test_a2a_envelope_defaults_to_steer_and_allows_explicit_queue(
    bridge_factory,
) -> None:
    # Arrange
    modes = []
    port = bridge_factory(
        lambda _text, **kwargs: modes.append(kwargs["delivery_mode"])
    )

    def envelope(dispatch_id: str, metadata: dict | None = None) -> dict:
        return {
            "jsonrpc": "2.0",
            "method": "SendMessage",
            "params": {
                "message": {"parts": [{"text": dispatch_id}]},
                "metadata": {"dispatch_id": dispatch_id, **(metadata or {})},
            },
        }

    # Act
    first_status, first = _post(port, "/v1/turn", envelope("default-steer"))
    _wait_exchange(port, first["exchange_id"])
    second_status, second = _post(
        port,
        "/v1/turn",
        envelope("explicit-queue", {"delivery_mode": "queue"}),
    )
    final = _wait_exchange(port, second["exchange_id"])

    # Assert
    assert (
        first_status,
        first["receipt"],
        second_status,
        second["receipt"],
        final["receipt"],
        modes,
    ) == (
        202,
        {"state": "pending", "final": False, "delivery_mode": "steer"},
        202,
        {"state": "pending", "final": False, "delivery_mode": "queue"},
        {"state": "delivered", "final": True},
        ["steer", "queue"],
    )


def test_turn_rejects_unknown_delivery_mode_before_opening_exchange(
    bridge_factory,
) -> None:
    # Arrange
    calls = []
    port = bridge_factory(lambda *_args, **_kwargs: calls.append(True))
    # Act
    status, body = _post(
        port, "/v1/turn", {"text": "hello", "delivery_mode": "maybe"}
    )
    # Assert
    assert (status, body, calls) == (
        400,
        {"error": "delivery_mode must be 'steer' or 'queue'"},
        [],
    )


def test_post_missing_text_field_returns_400(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda text, **_kw: None)
    # Act
    status, _body = _post(port, "/v1/turn", {"no_text_here": "x"})
    # Assert
    assert status == 400


def test_post_inject_failure_concludes_ordinary_exchange_with_502(
    bridge_factory,
) -> None:
    # Arrange
    def raise_session_gone(text: str, **_kw: object) -> None:
        raise RuntimeError("session gone")

    port = bridge_factory(raise_session_gone)
    # Act
    status, body = _post(port, "/v1/turn", {"text": "wake up"})
    final = _wait_exchange(port, body["exchange_id"])
    # Assert
    assert (
        status,
        final["status_code"]["code"],
        final["receipt"]["state"],
        final["receipt"]["final"],
        "session gone" in final["receipt"]["error"],
    ) == (202, 502, "failed", True, True)


@pytest.mark.parametrize("number,name", [(errno.ENOSPC, "ENOSPC"), (errno.EDQUOT, "EDQUOT")])
def test_bare_telegram_storage_failure_returns_structured_507(
    bridge_factory, number, name
) -> None:
    # Arrange
    def refuse_storage(_text: str, **_kw: object) -> None:
        raise OSError(number, "storage unavailable")

    port = bridge_factory(refuse_storage, agent_name="scitex-lead")
    telegram = (
        '<channel source="cct" chat_id="8379" message_id="19" row_id="9">\n'
        "hello\n</channel>"
    )

    # Act
    status, body = _post(port, "/v1/turn", {"text": telegram})

    # Assert
    assert (
        status,
        body["status_code"]["code"],
        body["check"]["ok"],
        body["check"]["cause"]["kind"],
        body["check"]["cause"]["code"],
    ) == (507, 507, False, "errno", name)


def test_bare_telegram_wake_returns_200_only_after_native_visibility(
    bridge_factory,
) -> None:
    # Arrange — CCT sends only text and treats any 2xx as final. The bridge
    # must generate a stable marker and wait for Hermes' native receipt.
    observed = {}

    def visible(text: str, **kwargs: object) -> object:
        observed["text"] = text
        observed.update(kwargs)
        return SimpleNamespace(status="steered", visibility="session.queue[0]")

    port = bridge_factory(visible, agent_name="scitex-lead")
    # Act
    telegram = (
        '<channel source="cct" chat_id="8379" message_id="9" row_id="3" '
        'user="ywatanabe">\ntelegram priority steer\n</channel>'
    )
    status, body = _post(port, "/v1/turn", {"text": telegram})
    # Assert
    generated_id = observed["visible_delivery_id"]
    assert (
        status,
        body["status_code"]["code"],
        body["agent"],
        body["exchange_id"].startswith("xch_"),
        body["delivery_id"],
        generated_id.startswith("cct_"),
        observed["text"],
    ) == (
        200,
        200,
        "scitex-lead",
        True,
        generated_id,
        True,
        telegram,
    )


def test_retried_bare_telegram_wake_does_not_submit_a_duplicate(
    bridge_factory,
) -> None:
    # Arrange — emulate CCT retrying the exact message after losing the first
    # HTTP response. The derived delivery identity must reopen the final row.
    submitted: list[str] = []

    def visible(text: str, **_kwargs: object) -> object:
        submitted.append(text)
        return SimpleNamespace(status="steered", visibility="session.queue[0]")

    port = bridge_factory(visible, agent_name="scitex-lead")
    # Act
    telegram = (
        '<channel source="cct" chat_id="8379" message_id="10" row_id="4">\n'
        "same telegram\n</channel>"
    )
    first_status, first = _post(port, "/v1/turn", {"text": telegram})
    retry_status, retry = _post(port, "/v1/turn", {"text": telegram})
    # Assert
    assert (
        first_status,
        retry_status,
        first["exchange_id"],
        retry["exchange_id"],
        retry["agent"],
        retry["delivery_id"],
        submitted,
    ) == (
        200,
        200,
        first["exchange_id"],
        first["exchange_id"],
        "scitex-lead",
        first["delivery_id"],
        [telegram],
    )


def test_same_telegram_identity_across_cct_source_aliases_is_deduplicated(
    bridge_factory,
) -> None:
    # Arrange — the wake rail authors source=cct while Claude Code labels the
    # CCT MCP notification with its server name. Telegram identity, not that
    # transport label or the body, is the stable cross-rail key.
    submitted: list[str] = []

    def visible(text: str, **_kwargs: object) -> object:
        submitted.append(text)
        return SimpleNamespace(status="steered", visibility="session.queue[0]")

    port = bridge_factory(visible, agent_name="scitex-hub")
    wake = (
        '<channel source="cct" chat_id="8379" message_id="10" row_id="4">\n'
        "same telegram\n</channel>"
    )
    native = wake.replace('source="cct"', 'source="claude-code-telegrammer"')

    # Act
    wake_status, wake_body = _post(port, "/v1/turn", {"text": wake})
    native_status, native_body = _post(port, "/v1/turn", {"text": native})

    # Assert
    assert (
        wake_status,
        native_status,
        wake_body["exchange_id"],
        native_body["exchange_id"],
        submitted,
    ) == (200, 200, wake_body["exchange_id"], wake_body["exchange_id"], [wake])


def test_identical_telegram_text_with_distinct_message_ids_is_not_deduplicated(
    bridge_factory,
) -> None:
    # Arrange
    submitted: list[str] = []

    def visible(text: str, **_kwargs: object) -> object:
        submitted.append(text)
        return SimpleNamespace(status="steered", visibility="session.queue[0]")

    port = bridge_factory(visible, agent_name="scitex-lead")
    first_text = (
        '<channel source="cct" chat_id="8379" message_id="10" row_id="4">\n'
        "hello\n</channel>"
    )
    second_text = (
        '<channel source="cct" chat_id="8379" message_id="11" row_id="5">\n'
        "hello\n</channel>"
    )
    # Act
    first_status, first = _post(port, "/v1/turn", {"text": first_text})
    second_status, second = _post(port, "/v1/turn", {"text": second_text})
    # Assert
    assert (
        first_status,
        second_status,
        first["exchange_id"] != second["exchange_id"],
        submitted,
    ) == (200, 200, True, [first_text, second_text])


def test_visible_delivery_failure_returns_actionable_durable_state(
    bridge_factory,
) -> None:
    # Arrange
    def refuse_visibility(text: str, **_kw: object) -> None:
        raise RuntimeError("Hermes composer contains staged operator input")

    port = bridge_factory(refuse_visibility, agent_name="scitex-hub")
    # Act
    status, body = _post(
        port,
        "/v1/turn",
        {
            "text": (
                '<channel source="operator" msg_id="m_retry">\nhello\n</channel>'
                "<!-- delivery:m_retry -->"
            ),
            "visible_delivery_id": "m_retry",
        },
    )
    probe = _wait_exchange(port, body["exchange_id"])
    # Assert
    assert (
        status,
        body["status_code"]["code"],
        probe["status_code"]["code"],
        "unconfirmed" in probe["status_code"]["message"],
    ) == (202, 202, 102, True)


def test_visible_delivery_retries_one_exchange_then_finishes_without_resubmit(
    bridge_factory,
) -> None:
    # Arrange: the first two native calls represent prompt acceptance followed
    # by transient projection misses.  On retry the native adapter's marker
    # precheck reports that same prompt already visible, so it must not submit
    # a duplicate (covered directly by test__hermes_tui_rpc as well).
    calls = {"count": 0}

    def transient_then_visible(text: str, **_kw: object) -> object:
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("gateway projection did not show the marker yet")
        return SimpleNamespace(
            status="already_visible",
            visibility="session.messages[4]",
        )

    port = bridge_factory(transient_then_visible, agent_name="scitex-hub")
    payload = {
        "text": "hello<!-- delivery:n_stable -->",
        "visible_delivery_id": "n_stable",
    }

    # Act
    first_status, first = _post(port, "/v1/turn", payload)
    first_probe = _wait_exchange(port, first["exchange_id"])
    second_status, second = _post(port, "/v1/turn", payload)
    second_probe = _wait_exchange(port, second["exchange_id"])
    third_status, third = _post(port, "/v1/turn", payload)
    final = _wait_exchange(port, third["exchange_id"])

    # Assert
    assert (
        first_status,
        first_probe["status_code"]["code"],
        second_status,
        second["exchange_id"],
        second_probe["status_code"]["code"],
        third_status,
        third["exchange_id"],
        final["status_code"]["code"],
        calls["count"],
        "no duplicate prompt" in final["status_code"]["message"],
    ) == (
        202,
        102,
        202,
        first["exchange_id"],
        102,
        202,
        first["exchange_id"],
        200,
        3,
        True,
    )


def test_visible_delivery_http_contract_reports_native_acceptance_and_proof(
    bridge_factory,
) -> None:
    # Arrange
    observed = {}

    def visible(text: str, **kwargs: object) -> object:
        observed["text"] = text
        observed.update(kwargs)
        return SimpleNamespace(
            status="steered",
            visibility="session.inflight.corrections",
        )

    port = bridge_factory(visible, agent_name="scitex-hub")
    message = (
        '<channel source="operator" msg_id="m_visible">\n'
        "Please inspect signup.\n</channel><!-- delivery:m_visible -->"
    )
    # Act
    status, body = _post(
        port,
        "/v1/turn",
        {
            "text": message,
            "from_agent": "operator",
            "visible_delivery_id": "m_visible",
            "delivery_mode": "steer",
        },
    )
    probe = _wait_exchange(port, body["exchange_id"])
    # Assert
    assert (
        status,
        body["status_code"]["code"],
        probe["status_code"]["code"],
        probe["status_code"]["message"],
        observed,
    ) == (
        202,
        202,
        200,
        "Hermes prompt.submit accepted the visible turn "
        "(status=steered, proof=session.inflight.corrections)",
        {
            "text": message,
            "from_agent": "operator",
            "dispatch_id": None,
            "visible_delivery_id": "m_visible",
            "delivery_mode": "steer",
        },
    )


def test_cards_exchange_id_is_preserved_through_visible_delivery(
    bridge_factory,
) -> None:
    # Arrange
    cards_exchange = "xch_20260912T000000Z_cards_abcdef"
    port = bridge_factory(lambda _text, **_kwargs: True, agent_name="scitex-hub")

    # Act
    status, body = _post(
        port,
        "/v1/turn",
        {
            "text": "<channel>hello</channel><!-- delivery:m_visible -->",
            "visible_delivery_id": "m_visible",
            "exchange_id": cards_exchange,
            "from_agent": "operator",
        },
    )
    final = _wait_exchange(port, cards_exchange)

    # Assert
    assert (status, body["exchange_id"], final["status_code"]["code"]) == (
        202,
        cards_exchange,
        200,
    )


def test_final_cards_exchange_retries_only_ack_without_duplicate_turn(
    bridge_factory,
) -> None:
    # Arrange: terminal delivery previously reached final 200, but the Cards
    # ACK was interrupted. Its unique marker may already have scrolled away.
    exchange_id = "xch_20260912T000000Z_cards_final"
    calls = []
    read_ids = []

    def open_existing(**_kwargs: object) -> tuple[str, str]:
        return exchange_id, "2026-09-12T00:00:00+00:00"

    def read_existing(received: str) -> dict | None:
        read_ids.append(received)
        return {
            "kind": "http",
            "code": 200,
            "message": "the incoming turn is visible in the Hermes transcript",
        }

    port = bridge_factory(
        lambda text, **kwargs: calls.append((text, kwargs)),
        agent_name="scitex-hub",
        open_exchange=open_existing,
        read_exchange=read_existing,
    )

    # Act
    status, body = _post(
        port,
        "/v1/turn",
        {
            "text": "do not inject twice <!-- delivery:n_final -->",
            "visible_delivery_id": "n_final",
            "exchange_id": exchange_id,
            "from_agent": "operator",
        },
    )

    # Assert
    assert (status, body["exchange_id"], calls, read_ids) == (
        202,
        exchange_id,
        [],
        [exchange_id],
    )


def test_turn_admission_coalesces_duplicate_and_rejects_overlap(bridge_factory):
    # Arrange
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_visible(text: str, **kwargs: object) -> bool:
        calls.append((text, kwargs))
        entered.set()
        release.wait(timeout=5)
        return True

    port = bridge_factory(blocking_visible, agent_name="scitex-hub")
    first_payload = {
        "text": "first <!-- delivery:n_first -->",
        "visible_delivery_id": "n_first",
    }

    # Act
    first_status, first = _post(port, "/v1/turn", first_payload)
    entered_before_followups = entered.wait(timeout=2)
    duplicate_status, duplicate = _post(port, "/v1/turn", first_payload)
    overlap_status, overlap = _post(
        port,
        "/v1/turn",
        {
            "text": "second <!-- delivery:n_second -->",
            "visible_delivery_id": "n_second",
        },
    )
    release.set()
    _wait_exchange(port, first["exchange_id"])

    # Assert
    assert (
        first_status,
        duplicate_status,
        duplicate["exchange_id"] == first["exchange_id"],
        overlap_status,
        overlap["status_code"]["code"],
        "retry" in overlap["status_code"]["message"],
        len(calls),
        entered_before_followups,
    ) == (202, 202, True, 429, 429, True, 1, True)


def test_post_unknown_route_returns_404(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda text, **_kw: None)
    # Act
    status, _body = _post(port, "/not/a/turn", {"text": "hi"})
    # Assert
    assert status == 404


def test_health_get_returns_200(bridge_factory) -> None:
    # Arrange
    port = bridge_factory(lambda text, **_kw: None)
    # Act
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
        status = resp.status
    # Assert
    assert status == 200


# ---------------------------------------------------------------------------
# Launcher — real spawn recorder, real PID file
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect HOME so the bridge's state-dir writes stay in the sandbox."""
    import os

    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_start_turn_bridge_noop_without_resolved_port(tmp_path: Path) -> None:
    # Arrange — an unresolved 'auto' port; spawn must never be invoked.
    def must_not_spawn(*_a, **_k):
        raise AssertionError("spawn must not run without a resolved a2a port")

    config = SimpleNamespace(
        a2a=SimpleNamespace(port="auto"),
        name="figrecipe",
        config_path=str(tmp_path / "spec.yaml"),
    )
    # Act
    pid = bridge.start_turn_bridge(config, spawn=must_not_spawn)
    # Assert
    assert pid is None


def test_start_turn_bridge_passes_resolved_port_to_spawn(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a resolved port + config_path; record the spawn argv.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="figrecipe", config_path=str(spec)
    )
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert str(_PORT) in recorded["argv"]


def test_start_turn_bridge_passes_effective_cards_store_to_child(
    tmp_path: Path, isolated_home: Path, env_save_restore
) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(_argv, **kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(pid=_PID)

    env_save_restore.set("SCITEX_STORE_DSN", "postgresql://wrong-shell:55432/store")
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT),
        apptainer=SimpleNamespace(raw_args=[]),
        env={"SCITEX_STORE_DSN": "postgresql://cards-store:55432/status"},
        labels={},
        name="figrecipe",
        config_path=str(spec),
    )
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert recorded["env"]["SCITEX_STORE_DSN"] == (
        "postgresql://cards-store:55432/status"
    )


def test_start_turn_bridge_returns_spawned_pid(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="figrecipe", config_path=str(spec)
    )
    # Act
    pid = bridge.start_turn_bridge(
        config,
        spawn=lambda argv, **kw: SimpleNamespace(pid=_PID),
        port_free_fn=_gate_says_free,
    )
    # Assert
    assert pid == _PID


def test_stop_turn_bridge_noop_when_no_pid_file(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — no bridge was ever started for this agent.
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="never-started", config_path=""
    )
    # Act
    stopped = bridge.stop_turn_bridge(config)
    # Assert
    assert stopped is False


# ---------------------------------------------------------------------------
# _build_on_turn — the inject callback (runtime DI seam, no tmux)
# ---------------------------------------------------------------------------
def test_build_on_turn_passes_text_and_wait_ready_false() -> None:
    # Arrange — a recording runtime whose send_turn reports delivered.
    seen: list = []

    def fake_send_turn(config, text, wait_ready):
        seen.append((text, wait_ready))
        return True

    runtime = SimpleNamespace(send_turn=fake_send_turn)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="a"), runtime=runtime)
    # Act
    on_turn("wake up")
    # Assert
    assert seen == [("wake up", False)]


def test_build_on_turn_preserves_native_visible_delivery_receipt() -> None:
    # Arrange
    receipt = SimpleNamespace(
        status="steered", visibility="session.inflight.corrections"
    )
    seen = []
    runtime = SimpleNamespace(
        send_visible_turn=lambda config, text, **kwargs: (
            seen.append((text, kwargs)) or receipt
        )
    )
    on_turn = bridge._build_on_turn(SimpleNamespace(name="a"), runtime=runtime)

    # Act
    result = on_turn("wake up", visible_delivery_id="n_visible")

    # Assert
    assert (result, seen) == (
        receipt,
        [
            (
                "wake up\n<!-- delivery:n_visible -->",
                {
                    "visible_delivery_id": "n_visible",
                    "delivery_mode": "steer",
                },
            )
        ],
    )


def test_build_on_turn_raises_when_session_absent() -> None:
    # Arrange — send_turn reports the session does not exist (returns False).
    runtime = SimpleNamespace(send_turn=lambda config, text, wait_ready: False)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="ghost"), runtime=runtime)
    # Act
    # Assert
    with pytest.raises(RuntimeError):
        on_turn("wake up")


# A refusal that explains itself and stops leaves the sender nowhere to go.
# scitex-hub hit exactly that on 2026-09-07: three identical 502s, then it
# reported "cannot reach sac" — correct against the old contract, and a system
# failure anyway. These pin the two things a sender cannot act without.


@pytest.fixture()
def refusal_message() -> str:
    """The RuntimeError text a REFUSED turn produces.

    Built once here so each test below asserts exactly one thing (STX-TQ007):
    a `pytest.raises` block counts as an assertion, so raising and inspecting
    in the same test is two.
    """
    runtime = SimpleNamespace(send_turn=lambda config, text, wait_ready: False)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="ghost"), runtime=runtime)
    try:
        on_turn("wake up")
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError("a refused turn must raise; it did not")


def test_the_refusal_says_NOTHING_WAS_QUEUED(refusal_message: str) -> None:
    # Arrange — the text a refused turn hands the sender.
    message = refusal_message

    # Act
    states_the_loss = "NOTHING WAS QUEUED" in message

    # Assert — the sender must not assume the turn is waiting somewhere.
    assert states_the_loss, message


def test_the_refusal_names_the_BUSY_next_step(refusal_message: str) -> None:
    # Arrange
    message = refusal_message

    # Act
    names_the_alternative = "durable rail" in message

    # Assert — busy wants "resend later / use a rail that survives a busy pane".
    assert names_the_alternative, message


def test_the_refusal_names_the_ABSENT_next_step_with_the_agent_name(
    refusal_message: str,
) -> None:
    # Arrange
    message = refusal_message

    # Act — the command must carry the NAME so it is runnable as printed.
    names_the_command = "sac agents start ghost" in message

    # Assert
    assert names_the_command, message


def test_CONTROL_a_DELIVERED_turn_raises_nothing() -> None:
    # Arrange — the same seam, but the pane accepts. A "next step" that also
    # appears on the success path would be noise, not guidance.
    runtime = SimpleNamespace(send_turn=lambda config, text, wait_ready: True)
    on_turn = bridge._build_on_turn(SimpleNamespace(name="ok"), runtime=runtime)

    # Act
    result = on_turn("wake up")

    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# Launcher — spawn-failure + real SIGTERM teardown
# ---------------------------------------------------------------------------
def test_start_turn_bridge_returns_none_on_spawn_failure(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — spawn raises; the launcher must swallow it and return None.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )

    def raising_spawn(argv, **kwargs):
        raise OSError("exec failed")

    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="boom", config_path=str(spec)
    )
    # Act
    pid = bridge.start_turn_bridge(
        config, spawn=raising_spawn, port_free_fn=_gate_says_free
    )
    # Assert
    assert pid is None


def test_stop_turn_bridge_sigterms_recorded_pid(isolated_home: Path) -> None:
    # Arrange — a REAL short-lived process whose PID is recorded as the bridge.
    proc = subprocess.Popen(["sleep", "30"])
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="kill-me", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    # Act
    stopped = bridge.stop_turn_bridge(config)
    # Assert
    assert stopped is True
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# start_turn_bridge — pre-existing-bridge teardown (a2a mis-route fix)
# ---------------------------------------------------------------------------
def test_start_turn_bridge_kills_preexisting_bridge_before_spawn(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a REAL prior bridge process recorded in the agent's pidfile
    # (the orphan a port-changing restart would otherwise leave alive), plus
    # a recording spawn for the NEW bridge so no second subprocess is created.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="restart-me", config_path=str(spec)
    )
    prior = subprocess.Popen(["sleep", "30"])
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(prior.pid), encoding="utf-8")
    # Act — start must SIGTERM the prior bridge before spawning the new one.
    bridge.start_turn_bridge(
        config,
        spawn=lambda argv, **kw: SimpleNamespace(pid=_PID),
        port_free_fn=_gate_says_free,
    )
    # Assert — the prior process received SIGTERM and exited.
    assert prior.wait(timeout=5) is not None
    # (defensive: make sure we never leak the helper if the assert above changes)
    if prior.poll() is None:  # pragma: no cover
        prior.kill()


def test_start_turn_bridge_records_new_pid_over_prior(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a stale pidfile pointing at an already-dead PID; the new
    # start must overwrite it with the freshly-spawned bridge's PID (never
    # leave the orphaned/stale value behind).
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=_PORT), name="repid-me", config_path=str(spec)
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999999", encoding="utf-8")  # dead/stale PID
    # Act
    bridge.start_turn_bridge(
        config,
        spawn=lambda argv, **kw: SimpleNamespace(pid=_PID),
        port_free_fn=_gate_says_free,
    )
    # Assert — pidfile now holds the new bridge's PID.
    assert pid_path.read_text(encoding="utf-8").strip() == str(_PID)


# ---------------------------------------------------------------------------
# Restart port-collision fix — SO_REUSEADDR + release/rebind/fail-loud
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """Return a currently-free ephemeral TCP port (bind :0, read, release)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


def _wait_until_bound(port: int, timeout_s: float = 5.0) -> None:
    """Block until ``port`` is no longer bindable (a listener has claimed it)."""
    deadline = time.monotonic() + timeout_s
    while bridge.port_is_free("127.0.0.1", port):
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def test_turn_bridge_server_sets_allow_reuse_address() -> None:
    # Arrange
    server_cls = bridge._TurnBridgeServer
    # Act
    reuse = server_cls.allow_reuse_address
    # Assert — SO_REUSEADDR so a rebind is not blocked by a TIME_WAIT socket.
    assert reuse is True


def test_build_server_rebind_after_close_succeeds() -> None:
    # Arrange — bind a real bridge server on an ephemeral port, then close it.
    first = bridge.build_server(
        host="127.0.0.1", port=0, on_turn=lambda *_a, **_k: None, agent_name="fig"
    )
    port = first.server_address[1]
    first.server_close()
    # Act — a fresh bind on the SAME port must succeed (allow_reuse_address).
    second = bridge.build_server(
        host="127.0.0.1", port=port, on_turn=lambda *_a, **_k: None, agent_name="fig"
    )
    rebound_port = second.server_address[1]
    second.server_close()
    # Assert
    assert rebound_port == port


def test_build_server_raises_named_error_when_port_held() -> None:
    # Arrange — a REAL socket holds the port so the bridge bind is refused.
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    # Act
    # Assert
    try:
        with pytest.raises(bridge.TurnBridgePortBusyError):
            bridge.build_server(
                host="127.0.0.1",
                port=port,
                on_turn=lambda *_a, **_k: None,
                agent_name="fig",
            )
    finally:
        held.close()


def test_start_turn_bridge_fails_loud_when_port_stays_busy(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a REAL listener holds the agent's a2a port; spawn must NOT run.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen()
    port = held.getsockname()[1]
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="stuck", config_path=str(spec)
    )
    clock = {"t": 0.0}

    def must_not_spawn(*_a, **_k):
        raise AssertionError("start must not spawn a bridge into a held port")

    # Act
    # Assert
    try:
        with pytest.raises(bridge.TurnBridgePortBusyError):
            bridge.start_turn_bridge(
                config,
                spawn=must_not_spawn,
                port_free_timeout_s=0.5,
                sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
                now_fn=lambda: clock["t"],
            )
    finally:
        held.close()


def test_stop_turn_bridge_releases_held_a2a_port(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — a REAL listener subprocess bound to the agent's a2a port,
    # recorded as the agent's bridge PID; stop must SIGTERM it AND wait for
    # the port to release (the incident: stop returned before release).
    port = _free_port()
    listener_code = (
        "import socket,time;"
        "s=socket.socket();"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('127.0.0.1',{port}));"
        "s.listen();"
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", listener_code])
    _wait_until_bound(port)
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="free-me", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    # Act
    bridge.stop_turn_bridge(config)
    proc.wait(timeout=5)
    # Assert — the a2a port is bindable again.
    assert bridge.port_is_free("127.0.0.1", port) is True


# ---------------------------------------------------------------------------
# Stop teardown — free the OWN port from a survivor that is NOT the tracked PID
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not _HAS_PORT_DISCOVERY,
    reason="needs lsof/ss/fuser to resolve the survivor holder for the sweep",
)
def test_stop_turn_bridge_force_frees_survivor_on_own_port(
    isolated_home: Path,
) -> None:
    # Arrange — a REAL survivor bound to the agent's OWN a2a port that is NOT
    # the tracked bridge PID and IGNORES SIGTERM (the 2026-07-12 incident:
    # await_bridge_release only reaps the tracked PID, so a survivor kept the
    # port and the next start failed loud). The tracked PID is already reaped,
    # so ONLY the force-kill sweep can free the port.
    port = _free_port()
    survivor_code = (
        "import socket,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "s=socket.socket();"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('127.0.0.1',{port}));"
        "s.listen();"
        "time.sleep(60)"
    )
    survivor = subprocess.Popen([sys.executable, "-c", survivor_code])
    _wait_until_bound(port)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)  # a tracked bridge PID that is already reaped
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="survivor-agent", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(dead.pid), encoding="utf-8")
    # Act
    try:
        bridge.stop_turn_bridge(config)
        survivor.wait(timeout=5)
        # Assert — the survivor on the OWN port was SIGKILLed; port bindable.
        assert bridge.port_is_free("127.0.0.1", port) is True
    finally:
        if survivor.poll() is None:  # pragma: no cover - defensive cleanup
            survivor.kill()
            survivor.wait(timeout=5)


def test_stop_turn_bridge_fails_loud_when_own_port_stays_stuck(
    isolated_home: Path,
) -> None:
    # Arrange — a probe that never reports the OWN a2a port free (a genuinely
    # unkillable holder). Even after the force-kill sweep, stop must FAIL LOUD
    # (raise the actionable TurnBridgePortBusyError) rather than silently
    # proceed into an EADDRINUSE crash on the next start. The tracked PID is
    # already reaped and the real port is free, so the sweep finds nothing to
    # kill and the injected probe drives the bounded re-poll to time out.
    port = _free_port()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    config = SimpleNamespace(
        a2a=SimpleNamespace(port=port), name="stuck-forever", config_path=""
    )
    pid_path = bridge._pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(dead.pid), encoding="utf-8")
    clock = {"t": 0.0}
    # Act
    # Assert
    with pytest.raises(bridge.TurnBridgePortBusyError):
        bridge.stop_turn_bridge(
            config,
            grace_s=0.5,
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
            now_fn=lambda: clock["t"],
            port_free_fn=lambda _h, _p: False,
        )


# ---------------------------------------------------------------------------
# spec.a2a.host -> the bridge's bind address
#
# The bridge is the SECOND of sac's three a2a bind paths. It hardcoded
# DEFAULT_HOST, so a spec declaring a reachable address bound loopback here
# while ``runtimes/a2a_sidecar.py`` alone honoured the declaration — and
# nothing reported the disagreement. Both directions are pinned: an UNCHANGED
# spec must still bind loopback, a CHANGED one must be followed.
#
# The bind assertions below observe the address the KERNEL reports for a REAL
# listening socket (``server_address`` after bind), not the argument passed in.
# ---------------------------------------------------------------------------

# A deliberately NON-loopback bind, the case the whole change exists for. Only
# the wildcard is bind-tested for real: a LAN literal is not guaranteed to
# exist on the machine running the suite.
_WILDCARD_HOST = "0.0.0.0"
_LAN_HOST = "192.168.11.23"


def _cfg_with_host(host: str | None, *, port: int = _PORT) -> SimpleNamespace:
    """A config whose ``a2a`` block declares ``host`` (or omits it for None)."""
    a2a = (
        SimpleNamespace(port=port)
        if host is None
        else SimpleNamespace(port=port, host=host)
    )
    return SimpleNamespace(a2a=a2a, name="figrecipe", config_path="")


def _observed_bind_host(config: SimpleNamespace) -> str:
    """Bind a REAL socket where the bridge resolves ``config`` to, and report it.

    Port 0 lets the kernel pick, so this never collides with a live agent; the
    returned value is ``server_address[0]`` — what the socket is ACTUALLY bound
    to, read back from the server rather than echoed from the input.
    """
    server = bridge.build_server(
        host=bridge.resolved_a2a_host(config),
        port=0,
        on_turn=lambda _text, **_kw: None,
        agent_name="figrecipe",
    )
    try:
        return str(server.server_address[0])
    finally:
        server.server_close()


def test_resolved_a2a_host_returns_the_declared_spec_host() -> None:
    # Arrange
    config = _cfg_with_host(_LAN_HOST)
    # Act
    host = bridge.resolved_a2a_host(config)
    # Assert
    assert host == _LAN_HOST


def test_resolved_a2a_host_defaults_to_loopback_when_undeclared() -> None:
    # Arrange — CASE 1 (no-regression): a spec with no host key at all.
    config = _cfg_with_host(None)
    # Act
    host = bridge.resolved_a2a_host(config)
    # Assert
    assert host == DEFAULT_A2A_HOST


def test_resolved_a2a_host_defaults_to_loopback_for_a_blank_host() -> None:
    # Arrange — a whitespace-only host states nothing and must not become an
    # unbindable empty string.
    config = _cfg_with_host("   ")
    # Act
    host = bridge.resolved_a2a_host(config)
    # Assert
    assert host == DEFAULT_A2A_HOST


def test_the_bridge_default_host_agrees_with_the_fleet_wide_default() -> None:
    # Arrange — the bridge carries its OWN "127.0.0.1" literal because the two
    # canonical spellings live in modules over this repo's line cap and cannot
    # be edited. Pin the agreement so a future drift breaks HERE, loudly,
    # instead of quietly splitting the fleet's bind address in two.
    pinned = DEFAULT_A2A_HOST
    # Act
    bridge_default = bridge.DEFAULT_HOST
    # Assert
    assert bridge_default == pinned


def test_bridge_binds_loopback_for_an_undeclared_spec_host() -> None:
    # Arrange — CASE 1 observed on a real socket.
    config = _cfg_with_host(None)
    # Act
    bound = _observed_bind_host(config)
    # Assert
    assert bound == DEFAULT_A2A_HOST


def test_bridge_binds_loopback_when_the_spec_declares_loopback() -> None:
    # Arrange — CASE 1 as all 102 fleet specs actually spell it today.
    config = _cfg_with_host(DEFAULT_A2A_HOST)
    # Act
    bound = _observed_bind_host(config)
    # Assert
    assert bound == DEFAULT_A2A_HOST


def test_bridge_binds_the_wildcard_address_the_spec_declares() -> None:
    # Arrange — CASE 2 observed on a real socket: the spec asks for every
    # interface and the kernel confirms the socket is there, not on loopback.
    config = _cfg_with_host(_WILDCARD_HOST)
    # Act
    bound = _observed_bind_host(config)
    # Assert
    assert bound == _WILDCARD_HOST


def test_start_turn_bridge_spawns_with_the_declared_host(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — CASE 2 through the LAUNCHER: the spawned bridge's --host must
    # carry the spec's value, or the subprocess binds somewhere else entirely.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **_kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = _cfg_with_host(_WILDCARD_HOST)
    config.config_path = str(spec)
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert recorded["argv"][recorded["argv"].index("--host") + 1] == _WILDCARD_HOST


def test_start_turn_bridge_spawns_with_loopback_for_an_undeclared_host(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — CASE 1 through the LAUNCHER.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **_kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = _cfg_with_host(None)
    config.config_path = str(spec)
    # Act
    bridge.start_turn_bridge(config, spawn=fake_spawn, port_free_fn=_gate_says_free)
    # Assert
    assert recorded["argv"][recorded["argv"].index("--host") + 1] == DEFAULT_A2A_HOST


def test_start_turn_bridge_explicit_host_overrides_the_spec(
    tmp_path: Path, isolated_home: Path
) -> None:
    # Arrange — the caller-supplied host is a seam and must still win over the
    # spec, so the spec default never removes an override.
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"), encoding="utf-8"
    )
    recorded: dict = {}

    def fake_spawn(argv, **_kwargs):
        recorded["argv"] = list(argv)
        return SimpleNamespace(pid=_PID)

    config = _cfg_with_host(_WILDCARD_HOST)
    config.config_path = str(spec)
    # Act
    bridge.start_turn_bridge(
        config,
        spawn=fake_spawn,
        host=DEFAULT_A2A_HOST,
        port_free_fn=_gate_says_free,
    )
    # Assert
    assert recorded["argv"][recorded["argv"].index("--host") + 1] == DEFAULT_A2A_HOST


# ---------------------------------------------------------------------------
# write_bridge_event — the lifecycle log (tui-turn-bridge.log was 0 bytes)
# ---------------------------------------------------------------------------
# Measured on the host 2026-08-11: 16 of 17 ``tui-turn-bridge.log`` files were
# EMPTY. The launcher opens the file and hands it to the child as stdout+stderr,
# but the bridge wrote nothing of its own, so the log only ever captured an
# unhandled traceback — and when 14 bridges were found dead, not one death
# could be explained. These tests pin the two lines that bracket a bridge's
# life. Real files, real fds; no mocks.


def test_write_bridge_event_records_the_event_name(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        line = bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert " bind " in line


def test_write_bridge_event_records_the_bound_port(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert f"port={_PORT}" in log_path.read_text(encoding="utf-8")


def test_write_bridge_event_records_the_pid(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert f"pid={_PID}" in log_path.read_text(encoding="utf-8")


def test_write_bridge_event_records_the_host(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
    # Assert
    assert f"host={DEFAULT_A2A_HOST}" in log_path.read_text(encoding="utf-8")


def test_write_bridge_event_flushes_so_a_crash_cannot_swallow_the_line(
    tmp_path: Path,
) -> None:
    # Arrange — read the file through a SEPARATE handle while the writer is
    # still open: only a real flush makes the bytes visible, which is what
    # keeps the bind line readable after an abrupt death.
    log_path = tmp_path / "tui-turn-bridge.log"
    observed = ""
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
        observed = log_path.read_text(encoding="utf-8")
    # Assert
    assert "tui-turn-bridge bind" in observed


def test_write_bridge_event_appends_two_lines_for_bind_then_shutdown(
    tmp_path: Path,
) -> None:
    # Arrange — the shutdown line must BRACKET the bind line in the same log.
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh, "bind", agent="figrecipe", host=DEFAULT_A2A_HOST, port=_PORT, pid=_PID
        )
        bridge.write_bridge_event(
            fh,
            "shutdown",
            agent="figrecipe",
            host=DEFAULT_A2A_HOST,
            port=_PORT,
            pid=_PID,
        )
    # Assert
    assert log_path.read_text(encoding="utf-8").count("\n") == 2


def test_write_bridge_event_names_the_agent(tmp_path: Path) -> None:
    # Arrange
    log_path = tmp_path / "tui-turn-bridge.log"
    # Act
    with log_path.open("w", encoding="utf-8") as fh:
        bridge.write_bridge_event(
            fh,
            "shutdown",
            agent="figrecipe",
            host=DEFAULT_A2A_HOST,
            port=_PORT,
            pid=_PID,
        )
    # Assert
    assert "agent=figrecipe" in log_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# extract_turn_text — the two body shapes that actually reach this bridge
# ---------------------------------------------------------------------------
def test_extract_turn_text_reads_the_flat_body() -> None:
    """The shape `sac listen` synthesises for a local wake."""
    # Arrange
    body = {"text": "hello", "from_agent": "peer"}
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text == "hello"


def test_extract_turn_text_reads_the_a2a_envelope() -> None:
    """The shape every a2a caller in this package sends.

    Regression for 2026-09-02: after the message:send route alias landed, a
    real peer message still bounced with `missing or empty text field`,
    because the bridge read only body["text"] while _wrap_message_send nests
    the content at params.message.parts[].text.
    """
    # Arrange
    body = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {"message": {"parts": [{"text": "from a peer"}]}, "metadata": {}},
    }
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text == "from a peer"


def test_extract_turn_text_returns_envelope_metadata_for_from_agent() -> None:
    """sac extension fields live under params.metadata, not at the root.

    A2A v1 rejects unknown fields at the params root, so the sender cannot put
    them where the flat reader looked; without this the inbound would be
    recorded with no requester and the completion report would be owed to
    nobody.
    """
    # Arrange
    body = {
        "params": {
            "message": {"parts": [{"text": "x"}]},
            "metadata": {"from_agent": "business", "dispatch_id": "d1"},
        }
    }
    # Act
    _text, meta = bridge.extract_turn_text(body)
    # Assert
    assert meta["from_agent"] == "business"


def test_extract_turn_text_joins_multipart_instead_of_dropping_the_tail() -> None:
    """Taking parts[0] would silently truncate a multi-part message."""
    # Arrange
    body = {"params": {"message": {"parts": [{"text": "a"}, {"text": "b"}]}}}
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text == "a\nb"


def test_extract_turn_text_rejects_an_empty_envelope() -> None:
    """An envelope with no usable text must still be refused, not injected."""
    # Arrange
    body = {"params": {"message": {"parts": [{"text": "   "}]}}}
    # Act
    text, _meta = bridge.extract_turn_text(body)
    # Assert
    assert text is None
