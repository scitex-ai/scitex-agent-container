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
    probe_listen_authed,
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
    # Three decimals: the authenticated probe answers in ~0.005s on the live
    # daemon, which two decimals rendered as "0.00s".
    assert "answered HTTP 401 in 0.180s" in message


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


# ---------------------------------------------------------------------------
# THE SECOND READING. scitex-dev asked for this twice: "the observation is
# cheap (it already probes /v1/health — probing one authenticated route
# alongside it would have shown this immediately)."
#
# Their samples, one daemon, one window:
#     POST /agents              -> timed out at 30s
#     POST /agents/<n>/send     -> timed out at 30s, TWICE consecutively
#     POST /v1/host_exec        -> HTTP 200 in ~2.4s
#     GET  /v1/health           -> 200 in 0.01s
# The failures track the /agents PREFIX, not authentication. And the same
# /agents/<n>/send had SUCCEEDED earlier that night, so the handler degrades
# rather than being dead — the shape that gets dismissed as a flake and
# "fixed" by a restart that was never needed.
#
# With two readings the message can stop saying "go measure" and say what was
# measured. These pin each of the four arms.
# ---------------------------------------------------------------------------


def _authed(status: int = 200, serving: bool = True) -> HealthProbe:
    return HealthProbe(
        serving=serving,
        status=status if serving else None,
        elapsed_s=2.40,
        error=None if serving else "timed out",
        url=f"{_BASE}/v1/host_exec/inflight",
        authenticated=True,
    )


def _message2(probe: HealthProbe, authed: HealthProbe | None) -> str:
    return transport_failure_message(
        verb="restart",
        name="neurovista",
        base=_BASE,
        route="POST /agents/neurovista/restart",
        exc=TimeoutError("timed out"),
        timeout_s=60.0,
        probe=probe,
        authed_probe=authed,
    )


def test_message_rules_out_daemon_wide_when_authed_answers() -> None:
    # Arrange — authenticated work IS being served, in the same seconds.
    #
    # This asserted on "the fault is specific to" and my own mutation control
    # caught it: that phrase ALSO appears in the no-second-reading arm, so the
    # test passed with the authed probe ignored entirely. It could not have
    # disagreed. "not daemon-wide" is the claim only this arm is entitled to
    # make, because only this arm measured a daemon serving authed work.
    #
    # 2026-08-11 — that last sentence was RIGHT and the message did not obey
    # it. The message went on to assert "THEREFORE the fault is specific to
    # <route>", which this arm is NOT entitled to: two fast control routes
    # rule OUT a daemon-wide fault, but cannot separate a wedged route from
    # one still working past the timeout. Measured that day: a spawn reported
    # as "no response within 30s" had been accepted and ran for 5m12s. The
    # test was more careful than the code; the code now matches the test.
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "not daemon-wide" in message


def test_message_refuses_a_restart_when_authed_answers() -> None:
    # Arrange — THE point of the second reading. A per-route fault must not
    # cost every agent on the box a restart; scitex-dev declined exactly that
    # remedy in July and was right to.
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "Do NOT run `sac listen restart`" in message


def test_message_warns_the_route_can_answer_then_degrade() -> None:
    # Arrange — measured intermittency: the same route succeeded earlier the
    # same night, so one later success is not proof of a fix.
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "does not mean it was fixed" in message


def test_message_refuses_to_call_the_route_faulty_when_authed_answers() -> None:
    # Arrange — the regression this arm exists to prevent. Ruling OUT a
    # daemon-wide fault is not ruling IN a route-specific one, and the old
    # message made exactly that leap in capitals ("THEREFORE the fault is
    # specific to <route>"). A reader acted on it within two minutes and
    # filed a P1 against the wrong component.
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "fault is specific to" not in message


def test_message_says_the_cause_is_not_established_when_authed_answers() -> None:
    # Arrange — the positive half of the arm above. Refusing the wrong claim
    # is only useful if the message also states, in the reader's own terms,
    # that the cause remains open.
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "NOT ESTABLISHED" in message


def test_message_names_agent_state_as_the_discriminator() -> None:
    # Arrange — a timeout on the transport says nothing about whether the
    # WORK happened. The only thing that does is the agent's own state, so
    # the message must send the reader there rather than leaving them with
    # an HTTP result they will over-read.
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "sac agents list neurovista" in message


def test_message_names_startup_failed_as_a_state_worth_reading() -> None:
    # Arrange — naming the command is not enough; the reader must know what
    # they are looking FOR. "startup_failed" is the state that distinguishes
    # "the spawn ran and lost" from "the spawn never began".
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "startup_failed" in message


def test_message_does_not_read_a_later_success_as_a_fix() -> None:
    # Arrange — measured intermittency has TWO readings: a genuine fault, or
    # an operation whose duration straddles the timeout. One later success
    # discriminates neither, and the message must say so rather than implying
    # the route "degrades".
    # Act
    message = _message2(_SERVING, _authed())
    # Assert
    assert "does not mean it was fixed" in message


def test_message_calls_the_fault_shared_when_authed_also_hangs() -> None:
    # Arrange — both an authenticated route and this one hung while the public
    # path answered. NOW a daemon-wide fault is the supported reading.
    # Act
    message = _message2(_SERVING, _authed(serving=False))
    # Assert
    assert "the fault is SHARED" in message


def test_message_recommends_a_restart_when_the_fault_is_shared() -> None:
    # Arrange — the restart is still reachable; it just has to be earned.
    # Act
    message = _message2(_SERVING, _authed(serving=False))
    # Assert
    assert "worth its cost" in message


def test_message_treats_a_401_on_the_authed_probe_as_no_reading() -> None:
    # Arrange — a 401 comes from the middleware BEFORE any handler runs, so it
    # proves the daemon is up and says NOTHING about authenticated work. Taking
    # it as "authenticated work is fine" would make this probe lie in exactly
    # the case it was added for.
    # Act
    message = _message2(_SERVING, _authed(status=401))
    # Assert
    assert "AUTH REJECTION" in message


def test_message_refuses_a_restart_on_an_auth_rejection() -> None:
    # Arrange — an unusable second reading is not evidence of a daemon fault.
    # Act
    message = _message2(_SERVING, _authed(status=401))
    # Assert
    assert "Do NOT restart the daemon on this" in message


def test_message_admits_it_measured_nothing_without_a_token() -> None:
    # Arrange — no bearer, so no second reading was even attempted.
    # Act
    message = _message2(_SERVING, None)
    # Assert
    assert "no authenticated route was measured" in message


# ---------------------------------------------------------------------------
# probe_listen_authed — what it actually sends
# ---------------------------------------------------------------------------


def test_authed_probe_returns_none_without_a_token() -> None:
    # Arrange — an honest "did not measure" beats a 401 the reader will
    # misread as a measurement.
    opener, _ = _opener_returning()
    # Act
    probe = probe_listen_authed(_BASE, None, opener=opener)
    # Assert
    assert probe is None


def test_authed_probe_sends_the_bearer() -> None:
    # Arrange — the whole point is to exercise the path /v1/health cannot.
    opener, captured = _opener_returning()
    # Act
    probe_listen_authed(_BASE, "tok-123", opener=opener)
    # Assert
    assert captured["headers"]["authorization"] == "Bearer tok-123"


def test_authed_probe_hits_a_route_off_the_agents_prefix() -> None:
    # Arrange — the observed failures track /agents, so a second reading on
    # /agents would measure the suspect rather than the control.
    opener, captured = _opener_returning()
    # Act
    probe_listen_authed(_BASE, "tok-123", opener=opener)
    # Assert
    assert captured["url"] == f"{_BASE}/v1/host_exec/inflight"


def test_authed_probe_never_executes_a_host_command() -> None:
    # Arrange — POST /v1/host_exec RUNS a command on the host and writes an
    # audit line. A probe that fires on every transport failure must not do
    # that, however convenient the route is.
    opener, captured = _opener_returning()
    # Act
    probe_listen_authed(_BASE, "tok-123", opener=opener)
    # Assert
    assert captured["method"] == "GET"


def test_authed_probe_reads_a_401_as_not_authorized() -> None:
    # Arrange — serving (an HTTP response arrived) but NOT authorized.
    opener = _opener_raising(_http_error(401))
    # Act
    probe = probe_listen_authed(_BASE, "bad-token", opener=opener)
    # Assert
    assert probe.authorized is False


def test_authed_probe_reads_a_200_as_authorized() -> None:
    # Arrange
    opener, _ = _opener_returning(status=200)
    # Act
    probe = probe_listen_authed(_BASE, "tok-123", opener=opener)
    # Assert
    assert probe.authorized is True
