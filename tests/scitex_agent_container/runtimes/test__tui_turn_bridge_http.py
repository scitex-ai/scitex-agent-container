"""TUI turn bridge (``runtimes._tui_turn_bridge``): the real HTTP turn and control routes.

Real seams only (PA-306 no-mocks); see test__tui_turn_bridge.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from scitex_agent_container.runtimes import _tui_turn_bridge as bridge
from tests.scitex_agent_container.runtimes._tui_turn_bridge_helpers import (
    _post,
    _wait_exchange,
)


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
    port = bridge_factory(lambda _text, **kwargs: modes.append(kwargs["delivery_mode"]))

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
    status, body = _post(port, "/v1/turn", {"text": "hello", "delivery_mode": "maybe"})
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
