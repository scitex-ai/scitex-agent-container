"""TUI turn bridge (``runtimes._tui_turn_bridge``): Telegram, visible and Cards-exchange delivery and admission.

Real seams only (PA-306 no-mocks); see test__tui_turn_bridge.py.
"""

from __future__ import annotations

import errno
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tests.scitex_agent_container.runtimes._tui_turn_bridge_helpers import (
    _post,
    _wait_delivery_idle,
    _wait_exchange,
)


@pytest.mark.parametrize(
    "number,name", [(errno.ENOSPC, "ENOSPC"), (errno.EDQUOT, "EDQUOT")]
)
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
    server = bridge_factory.server_at[port]
    payload = {
        "text": "hello<!-- delivery:n_stable -->",
        "visible_delivery_id": "n_stable",
    }

    # Act: each retry is posted only after the previous attempt released the
    # in-flight slot; an earlier POST is absorbed as "already in progress".
    first_status, first = _post(port, "/v1/turn", payload)
    _wait_delivery_idle(server, calls, attempts=1)
    first_probe = _wait_exchange(port, first["exchange_id"])
    second_status, second = _post(port, "/v1/turn", payload)
    _wait_delivery_idle(server, calls, attempts=2)
    second_probe = _wait_exchange(port, second["exchange_id"])
    third_status, third = _post(port, "/v1/turn", payload)
    final = _wait_exchange(
        port, third["exchange_id"], pending=(202, 102), timeout_s=10.0
    )

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
