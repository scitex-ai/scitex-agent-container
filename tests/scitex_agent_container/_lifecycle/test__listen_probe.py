"""Tests for the honest listen diagnosis — no mocks.

The production module exposes an injectable ``opener`` callable that
defaults to ``urllib.request.urlopen``; tests pass hand-rolled openers
returning real ``urllib``-shaped response objects (the same no-mocks
pattern as ``test__restart_client`` / ``test__spawn_client``).

What is being pinned here is a HONESTY contract, and it is worth stating
plainly. The old message asserted "the host listen broker is unreachable;
it may be flapping" on ANY transport failure. The reporter measured the
opposite: ``GET /health`` answered HTTP 401 in 0.18s (twice) while the
authenticated POST hung for 25s. The daemon was UP. So:

  * ANY HTTP response — including a 401 — proves the daemon is serving.
  * Only the ABSENCE of an HTTP exchange is evidence of "unreachable".
  * "flapping" is never asserted, because nothing here can observe a
    crash loop.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import io
from urllib import error as urlerror

from scitex_agent_container._lifecycle._listen_probe import (
    HealthProbe,
    probe_listen_health,
    transport_failure_message,
)

_BASE = "http://127.0.0.1:7878"


class _FakeResp:
    """A real callable response object matching the urllib contract."""

    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _opener_returning(status: int = 200):
    """Build (opener, captured) — the opener records the request it saw."""
    captured: dict = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in dict(req.headers).items()}
        captured["timeout"] = timeout
        return _FakeResp(status=status)

    return opener, captured


def _opener_raising(exc: Exception):
    def opener(req, timeout=None):
        raise exc

    return opener


def _http_error(status: int) -> urlerror.HTTPError:
    return urlerror.HTTPError(f"{_BASE}/v1/health", status, "nope", {}, io.BytesIO(b""))


# ---------------------------------------------------------------------------
# probe_listen_health — what did the daemon ACTUALLY do?
# ---------------------------------------------------------------------------


def test_probe_reads_http_200_as_serving() -> None:
    # Arrange
    opener, _ = _opener_returning(status=200)
    # Act
    probe = probe_listen_health(_BASE, opener=opener)
    # Assert
    assert probe.serving is True


def test_probe_reads_http_401_as_serving() -> None:
    # Arrange — the reporter's exact evidence: an unauthenticated GET got a
    # 401 in 0.18s. A 401 is a RESPONSE: the daemon is up and serving.
    opener = _opener_raising(_http_error(401))
    # Act
    probe = probe_listen_health(_BASE, opener=opener)
    # Assert
    assert probe.serving is True


def test_probe_keeps_the_http_status_it_saw() -> None:
    # Arrange
    opener = _opener_raising(_http_error(401))
    # Act
    probe = probe_listen_health(_BASE, opener=opener)
    # Assert
    assert probe.status == 401


def test_probe_reads_connection_refused_as_not_serving() -> None:
    # Arrange — no HTTP exchange at all. THIS, and only this, is
    # "unreachable".
    opener = _opener_raising(urlerror.URLError("connection refused"))
    # Act
    probe = probe_listen_health(_BASE, opener=opener)
    # Assert
    assert probe.serving is False


def test_probe_hits_the_public_health_path() -> None:
    # Arrange — /v1/health is the ONE path BearerAuthMiddleware exempts.
    opener, captured = _opener_returning()
    # Act
    probe_listen_health(_BASE, opener=opener)
    # Assert
    assert captured["url"] == f"{_BASE}/v1/health"


def test_probe_sends_no_authorization_header() -> None:
    # Arrange — the probe must exercise the path that CANNOT be wedged by a
    # starved worker pool. Sending the bearer would route it through the very
    # code it is trying to rule out, and it would hang with it.
    opener, captured = _opener_returning()
    # Act
    probe_listen_health(_BASE, opener=opener)
    # Assert
    assert "authorization" not in captured["headers"]


def test_probe_uses_a_short_timeout() -> None:
    # Arrange — the probe runs INSIDE a failure path that already burned its
    # own timeout; it must not add another 60s wait.
    opener, captured = _opener_returning()
    # Act
    probe_listen_health(_BASE, opener=opener)
    # Assert
    assert captured["timeout"] <= 5.0


def test_probe_never_raises_on_unexpected_error() -> None:
    # Arrange — a probe that exists to improve an error message must never
    # replace it with a crash of its own.
    opener = _opener_raising(RuntimeError("something exotic"))
    # Act
    probe = probe_listen_health(_BASE, opener=opener)
    # Assert
    assert probe.serving is False


# ---------------------------------------------------------------------------
# transport_failure_message — say what was measured, and nothing more
# ---------------------------------------------------------------------------


def _message(probe: HealthProbe) -> str:
    return transport_failure_message(
        verb="restart",
        name="neurovista",
        base=_BASE,
        route="POST /agents/neurovista/restart",
        exc=TimeoutError("timed out"),
        timeout_s=60.0,
        probe=probe,
    )


_SERVING = HealthProbe(
    serving=True,
    status=401,
    elapsed_s=0.18,
    error=None,
    url=f"{_BASE}/v1/health",
)
_DEAD = HealthProbe(
    serving=False,
    status=None,
    elapsed_s=0.01,
    error="connection refused",
    url=f"{_BASE}/v1/health",
)


def test_message_says_daemon_is_up_when_it_answered() -> None:
    # Arrange — daemon answered the cheap path; the authed route hung.
    # Act
    message = _message(_SERVING)
    # Assert
    assert "the listen daemon is UP and serving" in message


def test_message_blames_the_route_not_the_daemon() -> None:
    # Arrange — this test previously pinned the literal phrase "it is the
    # AUTHENTICATED route that did not answer". Its INTENT (attribute the
    # failure to the route, not the daemon) was right, but that wording
    # encoded the very claim scitex-dev refuted on 2026-08-04: that being
    # AUTHENTICATED is what distinguishes the hanging route. /v1/host_exec is
    # authenticated and answers fast, so it is not. Asserting the intent
    # instead keeps the guard and drops the theory.
    # Act
    message = _message(_SERVING)
    # Assert
    assert "this ONE route did not answer" in message.replace("\n", " ")


def test_message_quotes_the_measurement_it_made() -> None:
    # Arrange
    # Act
    message = _message(_SERVING)
    # Assert — the evidence is IN the message, so the operator can check it.
    assert "answered HTTP 401 in 0.18s" in message


def test_message_never_claims_flapping_when_daemon_answers() -> None:
    # Arrange — the word asserts a crash loop. Nothing here observed one.
    # Act
    message = _message(_SERVING)
    # Assert
    assert "flapping" not in message


def test_message_never_claims_flapping_when_daemon_is_down() -> None:
    # Arrange — even a genuinely dead daemon is not evidence of FLAPPING.
    # Act
    message = _message(_DEAD)
    # Assert
    assert "flapping" not in message


def test_message_says_cannot_reach_when_nothing_answered() -> None:
    # Arrange — no HTTP exchange on either route: genuinely unreachable.
    # Act
    message = _message(_DEAD)
    # Assert
    assert "cannot reach listen" in message


def test_message_keeps_the_listen_restart_remedy_when_down() -> None:
    # Arrange
    # Act
    message = _message(_DEAD)
    # Assert
    assert "sac listen restart" in message


# ---------------------------------------------------------------------------
# The message must REPORT what it observed, never NAME a cause it has not
# established (scitex-dev, 2026-08-04).
#
# This message has now carried TWO different wrong explanations. The first
# ("the host listen broker is unreachable; it may be flapping") was corrected
# on 2026-07-14 by replacing it with a second one: authenticated routes share a
# worker pool that /v1/health bypasses, therefore the pool is exhausted,
# therefore restart the daemon. That is also wrong — /v1/host_exec is
# AUTHENTICATED and answers in ~2.4s while /agents hangs, measured seconds
# apart on the same daemon, so a shared-pool exhaustion cannot wedge one and
# spare the other.
#
# Both times the fix was a better-sounding cause rather than removing the
# speculation, and both times the prescription was `sac listen restart` — a
# SHARED daemon restart that interrupts every other agent. scitex-dev declined
# it on those grounds and stayed blocked 11 days; I followed it and lost two
# remedies to a diagnosis that could not have been right.
#
# So the guard is not "do not say 'flapping'" or "do not say 'pool'" — a third
# wrong story would pass both. It is: the ONE observation this code has is an
# UNAUTHENTICATED health check, and it cannot single out a cause.
# ---------------------------------------------------------------------------


def test_message_does_not_blame_the_worker_pool() -> None:
    # Arrange — /v1/host_exec is authenticated AND fast, so pool exhaustion
    # cannot explain one authed route hanging while another answers.
    # Act
    message = _message(_SERVING)
    # Assert
    assert "worker pool" not in message


def test_message_admits_the_cause_is_not_established() -> None:
    # Arrange — one unauthenticated observation cannot identify a cause.
    # Act
    message = _message(_SERVING)
    # Assert
    assert "NOT ESTABLISHED" in message


def test_message_names_the_health_route_as_unauthenticated() -> None:
    # Arrange — the reader must know WHY the cheap probe proves so little.
    # Act
    message = _message(_SERVING)
    # Assert
    assert "UNAUTHENTICATED" in message


def test_message_offers_a_second_authed_route_as_the_next_step() -> None:
    # Arrange — the discriminator that separates "this handler" from "shared".
    # Act
    message = _message(_SERVING)
    # Assert
    assert "/v1/host_exec" in message


def test_message_warns_a_restart_interrupts_every_agent() -> None:
    # Arrange — the remedy's COST is what made following it blindly expensive.
    # Act
    message = _message(_SERVING)
    # Assert
    assert "interrupts EVERY agent" in message
