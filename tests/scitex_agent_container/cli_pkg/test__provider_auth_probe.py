"""The preflight must tell a WORKING key from a merely well-formed one.

MEASURED 2026-09-07 on scitex-compute-03, and the reason this probe exists:

  * ``handyman-c03-01`` declared ``auth_token_env: SAC_LOCAL_GPTOSS_KEY`` — a
    key named for GPT-OSS, a backend it no longer ran (its model was
    ``qwen38-27b``, served by the shared gateway, exactly like the agent
    beside it that worked).
  * That variable was UNSET in the login shell, but ``$HOME/.env`` carried it
    with a 13-character placeholder.
  * ``resolve_provider_api_key`` returned OK, len=13. It guards with
    ``if not api_key: raise`` — an emptiness test a placeholder passes.
  * The agent started, 401'd on every turn, and reported a green heartbeat.
  * ``sac agents check`` said "Ready to deploy."

30 specs across three hosts declared that variable, ``_template_handyman``
among them. On the one host whose ``$HOME/.env`` had no such line the same
specs refused to start — correct behaviour, and the evidence that the
placeholder is what disables the guard.

REAL HTTP SERVERS AND REAL ENV VARS, NO MOCKS. A mocked 401 would prove this
file's own bookkeeping; the thing under test is whether the probe reads a real
backend's real rejection, including the fact that ``urllib`` delivers a 401 by
RAISING ``HTTPError`` rather than returning a response.
"""

from __future__ import annotations

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from scitex_agent_container.cli_pkg._provider_auth_probe import (
    INDISCRIMINATE,
    OK,
    REJECTED,
    UNREACHABLE,
    models_url,
    probe_provider_auth,
)

_ENV_NAME = "SAC_TEST_PROVIDER_KEY"
_GOOD_KEY = "a-key-the-fake-backend-accepts"
_PLACEHOLDER = "sk-placeholder"


def _handler_for(*, accept_everything: bool):
    """A backend that either checks credentials or waves everything through."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            presented = (self.headers.get("Authorization") or "").removeprefix(
                "Bearer "
            )
            ok = accept_everything or presented == _GOOD_KEY
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args) -> None:
            """Silence the default stderr access log."""

    return _Handler


def _serve(*, accept_everything: bool):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _handler_for(accept_everything=accept_everything)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def discriminating_backend():
    """A backend that accepts only ``_GOOD_KEY`` — the normal gateway shape."""
    yield from _serve(accept_everything=False)


@pytest.fixture()
def permissive_backend():
    """A backend that answers 200 to anything — the control's target."""
    yield from _serve(accept_everything=True)


@pytest.fixture()
def provider_key():
    """Set the real env var the spec names, restoring what was there before.

    A real ``os.environ`` write rather than a patched lookup: the probe reaches
    the value through scitex-config's cascade, so a substituted resolver would
    test this file's idea of that cascade instead of the cascade.
    """
    previous = os.environ.get(_ENV_NAME)

    def _set(value: str) -> None:
        os.environ[_ENV_NAME] = value

    yield _set

    if previous is None:
        os.environ.pop(_ENV_NAME, None)
    else:
        os.environ[_ENV_NAME] = previous


def _config(base_url: str) -> SimpleNamespace:
    """A minimal spec shape: the probe reads it by duck-typed getattr."""
    return SimpleNamespace(
        name="probe-subject",
        claude=SimpleNamespace(
            provider=SimpleNamespace(
                base_url=base_url,
                auth_token_env=_ENV_NAME,
            )
        ),
    )


def _closed_port() -> int:
    """A port the OS confirmed free, released before returning."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_key_the_backend_ACCEPTS_passes(discriminating_backend, provider_key):
    # Arrange
    provider_key(_GOOD_KEY)

    # Act
    verdict = probe_provider_auth(_config(discriminating_backend), timeout=5)

    # Assert
    assert verdict.state == OK, verdict.detail


def test_a_PLACEHOLDER_the_backend_rejects_is_caught(
    discriminating_backend, provider_key
):
    # Arrange — the measured shape: non-empty, so the start-time emptiness
    # guard passes it, and wrong, so every turn 401s.
    provider_key(_PLACEHOLDER)

    # Act
    verdict = probe_provider_auth(_config(discriminating_backend), timeout=5)

    # Assert
    assert verdict.state == REJECTED, verdict.detail


def test_a_rejected_key_FAILS_the_preflight(discriminating_backend, provider_key):
    # Arrange
    provider_key(_PLACEHOLDER)

    # Act
    verdict = probe_provider_auth(_config(discriminating_backend), timeout=5)

    # Assert — the whole point: "Ready to deploy" must not survive this
    assert verdict.is_failure is True, verdict.detail


def test_the_rejection_names_the_ENV_VAR_so_the_fix_is_findable(
    discriminating_backend, provider_key
):
    # Arrange
    provider_key(_PLACEHOLDER)

    # Act
    verdict = probe_provider_auth(_config(discriminating_backend), timeout=5)

    # Assert — a verdict that does not say WHICH variable sends the reader
    # back to grep 30 specs by hand.
    assert _ENV_NAME in verdict.detail, verdict.detail


# --- THE POSITIVE CONTROL ---------------------------------------------------
# An "OK" from a backend that accepts anything would certify a dead key. The
# probe sends a deliberately invalid key too, and downgrades its own verdict
# when that is not rejected either.


def test_a_backend_that_accepts_ANY_key_yields_no_green_tick(
    permissive_backend, provider_key
):
    # Arrange — the real key is fine here, and would read as OK without the
    # control, because this backend answers 200 to everything.
    provider_key(_GOOD_KEY)

    # Act
    verdict = probe_provider_auth(_config(permissive_backend), timeout=5)

    # Assert
    assert verdict.state == INDISCRIMINATE, verdict.detail


def test_an_indiscriminate_backend_does_NOT_fail_the_preflight(
    permissive_backend, provider_key
):
    # Arrange — "I cannot tell" is not "the key is wrong". Failing here would
    # block every agent behind a backend with an open models endpoint.
    provider_key(_GOOD_KEY)

    # Act
    verdict = probe_provider_auth(_config(permissive_backend), timeout=5)

    # Assert
    assert verdict.is_failure is False, verdict.detail


# --- UNKNOWN NEVER REJECTS --------------------------------------------------


def test_an_unreachable_backend_reads_as_UNREACHABLE(provider_key):
    # Arrange — nothing is listening on this port.
    provider_key(_GOOD_KEY)
    dead = f"http://127.0.0.1:{_closed_port()}"

    # Act
    verdict = probe_provider_auth(_config(dead), timeout=1)

    # Assert
    assert verdict.state == UNREACHABLE, verdict.detail


def test_an_unreachable_backend_does_NOT_fail_the_preflight(provider_key):
    # Arrange — a gateway that is merely down must not fail every preflight on
    # the fleet, mirroring _check_host_route's refusal to convict on absent
    # evidence.
    provider_key(_GOOD_KEY)
    dead = f"http://127.0.0.1:{_closed_port()}"

    # Act
    verdict = probe_provider_auth(_config(dead), timeout=1)

    # Assert
    assert verdict.is_failure is False, verdict.detail


# --- URL COMPOSITION --------------------------------------------------------


def test_a_bare_origin_gets_the_v1_models_path():
    # Arrange — the shape every fleet spec carries.
    base = "http://host:18772"

    # Act
    url = models_url(base)

    # Assert
    assert url == "http://host:18772/v1/models", url


def test_a_base_url_ALREADY_ending_in_v1_is_not_doubled():
    # Arrange — appending blindly gives /v1/v1/models, whose 404 this module
    # would read as "reachable, not rejected": an OK verdict from an endpoint
    # that was never consulted.
    base = "http://host:18772/v1"

    # Act
    url = models_url(base)

    # Assert
    assert url == "http://host:18772/v1/models", url
