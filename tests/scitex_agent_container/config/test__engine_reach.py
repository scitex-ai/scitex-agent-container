"""``reach_verdict`` — the three shapes of "not 200" that are NOT one shape.

Real sockets, real DNS, a real HTTP server on a real loopback port. No mocks:
the whole value of this module is that it reports what the NETWORK actually
did, and a mock would only report what the test author believed it does.

The measurement it encodes (scitex-hub, 2026-09-05):

    scitex-compute-04:18772   HTTP 401   listening and auth-gating = REACHABLE
    compute-04:18772          000        the NAME does not resolve
    compute-04-lan:18772      000        the NAME does not resolve

``.invalid`` is reserved by RFC 2606 precisely so a test may rely on it not
resolving; the random-looking label guards against a wildcard resolver that
answers for everything under a real suffix.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scitex_agent_container.config._engine_reach import (
    REACH_NAME_UNRESOLVED,
    REACH_NO_HOST,
    REACH_REFUSED,
    REACH_UNAUTHORIZED,
    REACH_WRONG_PATH,
    ReachVerdict,
    reach_verdict,
)

UNRESOLVABLE = "http://gateway-6f3a9c2e4b71.invalid:18772"


class _Gated(BaseHTTPRequestHandler):
    """A gateway that is UP and demanding a key — the 401 the fleet measures."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; it is not the fact under test."""


@pytest.fixture
def gated_url():
    """A real HTTP server on a real port that answers 401 to everything."""
    server = HTTPServer(("127.0.0.1", 0), _Gated)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def closed_url():
    """A loopback port nothing is listening on — bound, read, then released.

    The bind is inside a ``with`` so the socket is released even if
    ``getsockname`` raises, and the fixture ``yield``s rather than returns:
    a fixture that acquires an external resource and hands it back with
    ``return`` has no teardown edge at all (STX-TQ005).
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    yield f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# 401 is the CORRECT answer for a live gateway
# ---------------------------------------------------------------------------


def test_a_401_is_named_reachable_but_unauthorized(gated_url) -> None:
    # Arrange
    url = gated_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.state == REACH_UNAUTHORIZED


def test_a_401_proves_the_endpoint_is_listening(gated_url) -> None:
    # Arrange — a check that treats non-2xx as failure calls this gateway dead.
    url = gated_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.proves_listening is True


def test_a_401_is_not_read_as_an_absent_endpoint(gated_url) -> None:
    # Arrange
    url = gated_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.proves_absent is False


def test_a_401_records_the_status_it_saw(gated_url) -> None:
    # Arrange
    url = gated_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.http_status == 401


def test_the_401_sentence_says_reachable_in_those_words(gated_url) -> None:
    # Arrange — a reader skims the sentence, not the enum member.
    url = gated_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert "reachable but unauthorized" in verdict.detail


# ---------------------------------------------------------------------------
# An unresolvable NAME is not a dead gateway
# ---------------------------------------------------------------------------


def test_an_unresolvable_name_is_named_as_such() -> None:
    # Arrange
    url = UNRESOLVABLE
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.state == REACH_NAME_UNRESOLVED


def test_an_unresolvable_name_is_not_evidence_the_gateway_is_down() -> None:
    # Arrange — curl prints 000 here AND for a dead host; this is the split.
    url = UNRESOLVABLE
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.proves_absent is False


def test_an_unresolvable_name_is_undetermined() -> None:
    # Arrange
    url = UNRESOLVABLE
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.undetermined is True


def test_the_unresolvable_sentence_says_the_name_does_not_resolve() -> None:
    # Arrange
    url = UNRESOLVABLE
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert "the NAME does not resolve" in verdict.detail


# ---------------------------------------------------------------------------
# A refusal is the one definite negative
# ---------------------------------------------------------------------------


def test_a_closed_port_is_named_connection_refused(closed_url) -> None:
    # Arrange
    url = closed_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.state == REACH_REFUSED


def test_a_closed_port_proves_the_endpoint_is_absent(closed_url) -> None:
    # Arrange
    url = closed_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.proves_absent is True


def test_a_closed_port_does_not_prove_listening(closed_url) -> None:
    # Arrange
    url = closed_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.proves_listening is False


def test_a_refusal_and_an_unresolvable_name_are_different_states(
    closed_url,
) -> None:
    # Arrange — the whole point of the module in one assertion.
    refused = reach_verdict(closed_url)
    # Act
    unresolved = reach_verdict(UNRESOLVABLE)
    # Assert
    assert refused.state != unresolved.state


# ---------------------------------------------------------------------------
# Degenerate input, and the closed enum
# ---------------------------------------------------------------------------


def test_a_url_with_no_host_is_named_rather_than_dialled() -> None:
    # Arrange
    url = "not-a-url"
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.state == REACH_NO_HOST


def test_an_unknown_state_cannot_be_constructed() -> None:
    # Arrange
    bogus = "probably-down"
    # Act
    construct = ReachVerdict
    # Assert
    with pytest.raises(ValueError):
        construct(url="http://x", state=bogus, detail="")


# ---------------------------------------------------------------------------
# A 404 is listening at the ADDRESS and says nothing about the PATH
#
# Measured from scitex-compute-04, 2026-09-06, against the fleet gateway:
#     /            404   listening, but this path does not exist
#     /v1/models   401   REACHABLE + AUTH-GATED — the informative answer
#     /health      200   a real health endpoint — a gate that cannot fail
# Folding the first into ``listening`` made a preflight of the gateway BASE
# report green from a path the gateway does not serve.
# ---------------------------------------------------------------------------


class _NotFound(BaseHTTPRequestHandler):
    """A process holding the port that does not serve the path asked for."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; it is not the fact under test."""


class _Open(BaseHTTPRequestHandler):
    """A gateway serving the path with no key demanded — 200, and reachable."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's own name
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args) -> None:
        """Silence the default stderr access log; it is not the fact under test."""


def _serving(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/models"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def missing_path_url():
    """A real HTTP server on a real port that answers 404 to everything."""
    yield from _serving(_NotFound)


@pytest.fixture
def open_url():
    """A real HTTP server on a real port that answers 200 to everything."""
    yield from _serving(_Open)


def test_a_404_is_named_listening_wrong_path(missing_path_url) -> None:
    # Arrange
    url = missing_path_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.state == REACH_WRONG_PATH


def test_a_404_does_not_prove_the_endpoint_is_served(missing_path_url) -> None:
    # Arrange — the whole defect: the gateway base answers exactly this.
    url = missing_path_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.serves_endpoint is False


def test_a_404_still_proves_something_holds_the_address(missing_path_url) -> None:
    # Arrange — a process DID answer, which is the weaker but true fact.
    url = missing_path_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.proves_listening is True


def test_a_404_is_not_read_as_an_absent_endpoint(missing_path_url) -> None:
    # Arrange
    url = missing_path_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.proves_absent is False


def test_a_404_and_a_401_are_different_states(missing_path_url, gated_url) -> None:
    # Arrange — one says the API is there and gating; the other says nothing.
    served = reach_verdict(gated_url)
    # Act
    missing = reach_verdict(missing_path_url)
    # Assert
    assert served.state != missing.state


def test_the_404_sentence_points_at_the_path_to_probe(missing_path_url) -> None:
    # Arrange — a reader skims the sentence, not the enum member.
    url = missing_path_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert "/v1/models" in verdict.detail


def test_a_401_proves_the_probed_path_is_served(gated_url) -> None:
    # Arrange — gated IS served; that is what an engine entry needs to see.
    url = gated_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.serves_endpoint is True


def test_a_200_proves_the_probed_path_is_served(open_url) -> None:
    # Arrange — an ungated /v1/models is reachable too.
    url = open_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.serves_endpoint is True


def test_a_closed_port_does_not_prove_the_probed_path_is_served(closed_url) -> None:
    # Arrange
    url = closed_url
    # Act
    verdict = reach_verdict(url)
    # Assert
    assert verdict.serves_endpoint is False
