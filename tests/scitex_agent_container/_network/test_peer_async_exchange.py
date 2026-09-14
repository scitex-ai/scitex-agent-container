"""Regression coverage for canonical asynchronous turn receipts."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

from scitex_agent_container._network.peer import (
    PeerError,
    PeerTimeoutPending,
    post_turn_to_url,
)

EXCHANGE_ID = "xch_20260913T000000Z_test_abcdef"


@contextmanager
def _exchange_server(
    *,
    receipt_code: int = 202,
    receipt_body: dict | None = None,
    results: list[dict] | None = None,
) -> Iterator[tuple[str, list[str]]]:
    paths: list[str] = []
    bodies = list(results or [])
    receipt = receipt_body or {
        "exchange_id": EXCHANGE_ID,
        "receipt": {"state": "pending", "final": False, "delivery_mode": "steer"},
        "status_code": {
            "kind": "http",
            "code": 202,
            "message": f"accepted; poll /v1/exchanges/{EXCHANGE_ID}",
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def _write(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            paths.append(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self._write(receipt_code, receipt)

        def do_GET(self) -> None:  # noqa: N802
            paths.append(self.path)
            result = bodies.pop(0) if len(bodies) > 1 else bodies[0]
            self._write(200, result)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/turn", paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _result(code: int, message: str) -> dict:
    final = code not in {102, 202}
    state = (
        "retryable"
        if code == 102
        else "pending"
        if code == 202
        else "delivered"
        if code == 200
        else "failed"
    )
    return {
        "exchange_id": EXCHANGE_ID,
        "receipt": {"state": state, "final": final},
        "status_code": {"kind": "http", "code": code, "message": message},
    }


def _capture_error(url: str, *, timeout_s: float = 2) -> PeerError:
    try:
        post_turn_to_url(url, "hello", timeout_s=timeout_s)
    except PeerError as exc:
        return exc
    raise AssertionError("post_turn_to_url did not raise PeerError")


def test_async_receipt_is_polled_to_confirmed_delivery() -> None:
    # Arrange
    with _exchange_server(results=[_result(200, "the adapter accepted the turn")]) as (
        url,
        paths,
    ):
        # Act
        response = post_turn_to_url(url, "hello", timeout_s=2)
    # Assert
    assert (
        "Turn accepted" in response,
        "not agent completion" in response,
        paths,
    ) == (True, True, ["/v1/turn", f"/v1/exchanges/{EXCHANGE_ID}"])


def test_nonfinal_exchange_is_polled_until_final() -> None:
    # Arrange
    results = [
        _result(102, f"still delivering; poll `/v1/exchanges/{EXCHANGE_ID}`"),
        _result(200, "accepted"),
    ]
    with _exchange_server(results=results) as (url, paths):
        # Act
        response = post_turn_to_url(url, "hello", timeout_s=2)
    # Assert
    assert (EXCHANGE_ID in response, paths.count(f"/v1/exchanges/{EXCHANGE_ID}")) == (
        True,
        2,
    )


def test_ssh_turn_receipt_is_polled_over_the_same_transport(ssh_http_shim) -> None:
    # Arrange
    ssh_http_shim.install()
    with _exchange_server(results=[_result(200, "accepted")]) as (local_url, _paths):
        port = local_url.split(":", 2)[2].split("/", 1)[0]
        # Act
        response = post_turn_to_url(
            f"ssh://compute-03:{port}/v1/turn", "hello", timeout_s=5
        )

    calls = ssh_http_shim.invocations()
    # Assert
    assert (
        "Turn accepted" in response,
        [call["method"] for call in calls],
        calls[1]["path"],
    ) == (True, ["POST", "GET"], f"/v1/exchanges/{EXCHANGE_ID}")


def test_receipt_requires_actual_http_202_on_direct_transport() -> None:
    # Arrange
    with _exchange_server(receipt_code=200, results=[_result(200, "accepted")]) as (
        url,
        paths,
    ):
        # Act
        error = _capture_error(url)
    # Assert
    assert ("expected HTTP 202" in str(error), paths) == (True, ["/v1/turn"])


def test_malformed_receipt_names_the_acceptance_contract() -> None:
    # Arrange
    bad_receipt = {
        "exchange_id": EXCHANGE_ID,
        "status_code": {"kind": "http", "code": 200, "message": "wrong"},
    }
    with _exchange_server(receipt_body=bad_receipt, results=[_result(200, "ok")]) as (
        url,
        paths,
    ):
        # Act
        error = _capture_error(url)
    # Assert
    assert ("status_code=http/202" in str(error), paths) == (True, ["/v1/turn"])


def test_receipt_rejects_noncanonical_exchange_identifier() -> None:
    # Arrange
    bad_receipt = {
        "exchange_id": "request-123",
        "status_code": {
            "kind": "http",
            "code": 202,
            "message": "accepted; poll `/v1/exchanges/request-123`",
        },
    }
    with _exchange_server(receipt_body=bad_receipt, results=[_result(200, "ok")]) as (
        url,
        paths,
    ):
        # Act
        error = _capture_error(url)
    # Assert
    assert ("canonical xch_" in str(error), paths) == (True, ["/v1/turn"])


def test_synchronous_text_reply_is_not_a_delivery_receipt() -> None:
    # Arrange
    with _exchange_server(
        receipt_code=200,
        receipt_body={"text": "looks successful"},
        results=[_result(200, "unused")],
    ) as (url, paths):
        # Act
        error = _capture_error(url)
    # Assert
    assert ("expected HTTP 202" in str(error), paths) == (True, ["/v1/turn"])


def test_failed_exchange_surfaces_final_message_and_probe_hint() -> None:
    # Arrange
    failure = _result(502, "terminal visibility was not confirmed")
    with _exchange_server(results=[failure]) as (url, _paths):
        # Act
        error = _capture_error(url)
    detail = str(error)
    # Assert
    assert (
        "http/502" in detail,
        "terminal visibility was not confirmed" in detail,
        f"curl -sS {url.removesuffix('/v1/turn')}/v1/exchanges/" in detail,
    ) == (True, True, True)


def test_nonfinal_timeout_preserves_exchange_and_says_not_to_resend() -> None:
    # Arrange
    with _exchange_server(
        results=[_result(102, f"still delivering; poll `/v1/exchanges/{EXCHANGE_ID}`")]
    ) as (
        url,
        _paths,
    ):
        # Act
        error = _capture_error(url, timeout_s=0.03)
    # Assert
    assert (
        isinstance(error, PeerTimeoutPending),
        getattr(error, "status", None),
        EXCHANGE_ID in str(error),
        "do not resend" in str(error),
        "curl -sS" in str(error),
        getattr(error, "exchange_id", None),
        "v1/exchanges" in str(getattr(error, "poll_hint", "")),
    ) == (
        True,
        "exchange_pending",
        True,
        True,
        True,
        EXCHANGE_ID,
        True,
    )


def test_receipt_projection_cannot_contradict_status_primitive() -> None:
    # Arrange
    contradictory = _result(200, "delivered")
    contradictory["receipt"] = {"state": "pending", "final": False}
    with _exchange_server(results=[contradictory]) as (url, _paths):
        # Act
        error = _capture_error(url)
    # Assert
    assert "contradictory receipt" in str(error)
