"""The watchdog's DECISION model — does it restart, and on what evidence?

Incident 2026-07-14: the watchdog RESTARTED A HEALTHY CONTROL PLANE. It
probed ``/v1/health`` with a 5-SECOND curl deadline on a box that idles at
load 60-70, and restarted ``sac-listen.service`` after ONE failure. Every
``sac listen`` restart tears down the in-memory a2a Broker, which DEAFENS
EVERY AGENT'S INBOX AT ONCE — so a slow probe did not merely mis-report, it
MANUFACTURED the outage it claimed to detect, then re-probed during its own
restart and restarted AGAIN (2 restarts in 26s, live journal).

Measured against the UNFIXED probe, with a REAL server answering HTTP 200 in
8s (healthy, merely busy): **3 restarts out of 3 probes.**

Both directions are bugs. These tests pin BOTH:

* it must NOT restart on a slow-but-alive daemon, a single failure, an
  uncorroborated verdict, during its own restart, or past the restart cap;
* it MUST still restart a genuinely dead or wedged one (incident 2026-06-26:
  listen died with nothing restarting it and the fleet was cut off silently).

No mocks (STX-NM002). Every case drives the REAL probe script against a REAL
HTTP server on a REAL ephemeral loopback socket — slow, refusing, 5xx-ing,
401-ing, dying. The "refused" case points at a genuinely closed port. Nothing
here ever touches port 7878: that is the live control plane the whole fleet
depends on, and a test that restarts it would repeat the incident.

AAA markers (TQ002); 3+-word test names.
"""

from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE = _REPO_ROOT / "scripts" / "systemd" / "sac-listen-health-probe.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None,
    reason="sac-listen-health-probe.sh requires curl",
)

# A unit that does not exist. We assert the DECISION (the loud ERROR lines),
# never a real systemctl effect — the probe must never restart anything real
# from a test.
_FAKE_UNIT = "sac-listen-watchdog-pytest-nonexistent.service"


# --- real servers ---------------------------------------------------------


def _handler(status: int, delay: float):
    """A REAL handler answering `status` after `delay` seconds."""

    class _H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802 (http.server API)
            if delay:
                # A HEALTHY daemon that is merely BUSY: it does answer.
                threading.Event().wait(delay)
            body = b'{"ok": true, "service": "sac-listen"}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            return

    return _H


class _Server:
    """A real HTTP server on a real ephemeral loopback port (never 7878)."""

    def __init__(self, status: int = 200, delay: float = 0.0):
        self._srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler(status, delay)
        )
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/health"

    def kill(self) -> None:
        """Really stop serving — the port goes to connection-refused."""
        self._srv.shutdown()
        self._srv.server_close()


def _closed_port() -> int:
    """Bind then release a port, so nothing is listening on it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- driving the real probe -----------------------------------------------


class _Probe:
    """Runs the REAL shipped probe against `url`, with a private ledger."""

    def __init__(self, tmp_path: Path, url: str, **env):
        self.state = tmp_path / "listen-health.state"
        self.url = url
        self.env = {
            "SAC_LISTEN_HEALTH_URL": url,
            "SAC_LISTEN_HEALTH_STATE": str(self.state),
            "SAC_LISTEN_UNIT": _FAKE_UNIT,
            "SAC_LISTEN_NOTIFY": "0",  # never fire the operator alarm
            "SAC_LISTEN_PROBE_TIMEOUT": "3",  # keep the suite fast
            "SAC_LISTEN_CONNECT_TIMEOUT": "2",
            **{k: str(v) for k, v in env.items()},
        }

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(self.env)
        return subprocess.run(
            ["bash", str(_PROBE), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def restarted(self, *args: str) -> bool:
        """True iff this invocation DECIDED to restart the listen."""
        return "RESTARTING" in self.run(*args).stderr


# ==========================================================================
# THE HEADLINE BUG: a slow-but-ALIVE daemon must never be restarted
# ==========================================================================


@pytest.fixture()
def loaded_box(tmp_path):
    """The incident, exactly: a HEALTHY daemon answering HTTP 200 in 8s.

    Deliberately driven at the probe's SHIPPED DEFAULT deadline — no
    override. That is the whole point: the old default (5s) restarted this
    server 3 times out of 3; the new default (20s) sees it for what it is.
    A test that overrode the deadline would pass on the BROKEN script too,
    and prove nothing.
    """
    srv = _Server(status=200, delay=8.0)
    probe = _Probe(tmp_path, srv.url)
    del probe.env["SAC_LISTEN_PROBE_TIMEOUT"]
    del probe.env["SAC_LISTEN_CONNECT_TIMEOUT"]
    try:
        yield probe
    finally:
        srv.kill()


def test_slow_but_alive_is_never_restarted(loaded_box):
    # Arrange
    probe = loaded_box
    # Act — three consecutive probes, as the timer would fire them.
    decisions = [probe.restarted() for _ in range(3)]
    # Assert — the daemon ANSWERED every time. It must not be destroyed.
    assert decisions == [False, False, False]


def test_slow_but_alive_probe_stays_quiet(loaded_box):
    # Arrange
    probe = loaded_box
    # Act
    result = probe.run()
    # Assert — a healthy 30s probe must not spam the journal.
    assert result.returncode == 0 and result.stderr.strip() == ""


# ==========================================================================
# THREE STATES: a timeout is UNKNOWN, not DOWN
# ==========================================================================


def test_timeout_is_unknown_not_down(tmp_path):
    # Arrange — answers, but SLOWER than the deadline: we get nothing back.
    srv = _Server(status=200, delay=5.0)
    probe = _Probe(tmp_path, srv.url, SAC_LISTEN_PROBE_TIMEOUT=1)
    try:
        # Act
        result = probe.run("--check-only")
    finally:
        srv.kill()
    # Assert — "I asked and got nothing" is UNKNOWN. Never reported as DOWN.
    assert "UNKNOWN" in result.stderr and "DOWN" not in result.stderr


def test_timeout_reports_tcp_did_connect(tmp_path):
    # Arrange — the port IS bound; only the HTTP answer is missing.
    srv = _Server(status=200, delay=5.0)
    probe = _Probe(tmp_path, srv.url, SAC_LISTEN_PROBE_TIMEOUT=1)
    try:
        # Act
        result = probe.run("--check-only")
    finally:
        srv.kill()
    # Assert — the evidence must say the socket was reachable, which is what
    # distinguishes "busy" from "gone".
    assert "the port IS bound" in result.stderr


def test_refused_is_reported_as_down(tmp_path):
    # Arrange — a genuinely closed port: the kernel sends RST.
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    # Act
    result = probe.run("--check-only")
    # Assert
    assert "DOWN" in result.stderr and "connection refused" in result.stderr


def test_single_timeout_does_not_restart(tmp_path):
    # Arrange
    srv = _Server(status=200, delay=5.0)
    probe = _Probe(tmp_path, srv.url, SAC_LISTEN_PROBE_TIMEOUT=1)
    try:
        # Act — ONE failed probe. On a loaded box this means nothing.
        decided = probe.restarted()
    finally:
        srv.kill()
    # Assert
    assert decided is False


# ==========================================================================
# ANY HTTP STATUS < 500 IS "UP" — the bearer-auth lesson (PR #463)
# ==========================================================================


@pytest.mark.parametrize("status", [200, 204, 301, 401, 403, 404])
def test_any_non_server_error_status_is_up(tmp_path, status):
    # Arrange — a 401 PROVES the daemon is up: bound, speaking HTTP,
    # auth-gating. Gating liveness on 200 once SIGKILLed a healthy daemon.
    srv = _Server(status=status)
    probe = _Probe(tmp_path, srv.url)
    try:
        # Act
        result = probe.run("--check-only")
    finally:
        srv.kill()
    # Assert
    assert result.returncode == 0


@pytest.mark.parametrize("status", [401, 403])
def test_auth_gated_daemon_is_never_restarted(tmp_path, status):
    # Arrange
    srv = _Server(status=status)
    probe = _Probe(tmp_path, srv.url)
    try:
        # Act — probe repeatedly; a 401 must never accrue a failure.
        decisions = [probe.restarted() for _ in range(4)]
    finally:
        srv.kill()
    # Assert
    assert not any(decisions)


def test_server_error_is_a_failure_not_up(tmp_path):
    # Arrange — it ANSWERED, with a 500. Bound and speaking HTTP, but its
    # health route is erroring: an answer, but not health.
    srv = _Server(status=500)
    probe = _Probe(tmp_path, srv.url)
    try:
        # Act
        result = probe.run("--check-only")
    finally:
        srv.kill()
    # Assert
    assert result.returncode == 1


def test_single_server_error_does_not_restart(tmp_path):
    # Arrange — a 5xx is a SOFT failure: the daemon is demonstrably alive, so
    # destroying it demands full corroboration.
    srv = _Server(status=500)
    probe = _Probe(tmp_path, srv.url)
    try:
        # Act
        decided = probe.restarted()
    finally:
        srv.kill()
    # Assert
    assert decided is False


# ==========================================================================
# CORROBORATION — N consecutive failures before the remedy
# ==========================================================================


def test_first_refusal_does_not_restart(tmp_path):
    # Arrange
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    # Act
    decided = probe.restarted()
    # Assert — even a HARD down (weight 2) is below the threshold (3) alone.
    assert decided is False


def test_second_refusal_restarts_dead_listen(tmp_path):
    # Arrange — a genuinely dead listen MUST still come back
    # (incident 2026-06-26). Crash coverage is not weakened.
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    # Act
    first = probe.restarted()
    second = probe.restarted()
    # Assert — refusal counts HARDER than a timeout, so 2 probes suffice.
    assert (first, second) == (False, True)


def test_three_timeouts_restart_wedged_listen(tmp_path):
    # Arrange — a WEDGED daemon: bound, accepting TCP, never answering. This
    # is the case systemd's Restart=always CANNOT see, and the reason the
    # watchdog exists. It must still be healed.
    srv = _Server(status=200, delay=30.0)
    probe = _Probe(tmp_path, srv.url, SAC_LISTEN_PROBE_TIMEOUT=1)
    try:
        # Act
        decisions = [probe.restarted() for _ in range(3)]
    finally:
        srv.kill()
    # Assert — 3 consecutive UNKNOWNs corroborate into a DOWN verdict.
    assert decisions == [False, False, True]


def test_two_timeouts_do_not_yet_restart(tmp_path):
    # Arrange
    srv = _Server(status=200, delay=30.0)
    probe = _Probe(tmp_path, srv.url, SAC_LISTEN_PROBE_TIMEOUT=1)
    try:
        # Act
        decisions = [probe.restarted() for _ in range(2)]
    finally:
        srv.kill()
    # Assert — below threshold: the fleet is not sacrificed to a maybe.
    assert decisions == [False, False]


def test_uncorroborated_failure_says_not_restarting(tmp_path):
    # Arrange
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    # Act
    result = probe.run()
    # Assert — it must SAY it saw a failure and chose not to act.
    assert "NOT restarting" in result.stderr and "2/3" in result.stderr


# ==========================================================================
# A FAILURE IS A FACT — one lucky reply must not wipe the ledger
# (consistent with _listen/_standby_ledger.py, PR #673)
# ==========================================================================


def test_one_success_does_not_wipe_failure_streak(tmp_path):
    # Arrange — the bug PR #673 fixed in sac listen's own holder check: a
    # single reply reset `consecutive_unhealthy = 0`, so a FLAPPING daemon
    # oscillated 1/2 -> "healthy" -> 1/2 forever and was NEVER acted on.
    dead = f"http://127.0.0.1:{_closed_port()}/v1/health"
    srv = _Server(status=200)
    live = srv.url
    probe = _Probe(tmp_path, dead)
    try:
        # Act — fail (weight 2), then one lucky success, then fail again.
        first = probe.restarted()
        probe.env["SAC_LISTEN_HEALTH_URL"] = live
        probe.run()  # a single UP: must NOT clear the standing failure
        probe.env["SAC_LISTEN_HEALTH_URL"] = dead
        third = probe.restarted()
    finally:
        srv.kill()
    # Assert — the ledger survived the blip, so the flapper is acted on.
    assert (first, third) == (False, True)


def test_single_success_keeps_ledger_standing(tmp_path):
    # Arrange
    dead = f"http://127.0.0.1:{_closed_port()}/v1/health"
    srv = _Server(status=200)
    probe = _Probe(tmp_path, dead)
    try:
        probe.run()  # accrue a failure
        probe.env["SAC_LISTEN_HEALTH_URL"] = srv.url
        # Act
        result = probe.run()
    finally:
        srv.kill()
    # Assert — it must SAY the ledger still stands, loudly.
    assert "still stands" in result.stderr


def test_sustained_recovery_clears_the_ledger(tmp_path):
    # Arrange — a daemon that blips once and then genuinely recovers must NOT
    # be destroyed. Two consecutive answers clear it.
    dead = f"http://127.0.0.1:{_closed_port()}/v1/health"
    srv = _Server(status=200)
    probe = _Probe(tmp_path, dead)
    try:
        probe.run()  # failure weight 2
        probe.env["SAC_LISTEN_HEALTH_URL"] = srv.url
        probe.run()  # UP 1/2 — not yet cleared
        # Act
        result = probe.run()  # UP 2/2 — cleared
    finally:
        srv.kill()
    # Assert — and the clearing is LOUD: "the thing I said was broken now
    # looks fine" is exactly what an operator must never have hidden.
    assert "RECOVERED" in result.stderr


def test_recovered_daemon_survives_a_later_blip(tmp_path):
    # Arrange — after a genuine recovery the ledger is clean, so a single
    # later failure is once again just a blip, not a death sentence.
    dead = f"http://127.0.0.1:{_closed_port()}/v1/health"
    srv = _Server(status=200)
    probe = _Probe(tmp_path, dead)
    try:
        probe.run()  # fail (2)
        probe.env["SAC_LISTEN_HEALTH_URL"] = srv.url
        probe.run()  # UP 1/2
        probe.run()  # UP 2/2 -> cleared
        probe.env["SAC_LISTEN_HEALTH_URL"] = dead
        # Act
        decided = probe.restarted()  # fail (2) — from a CLEAN ledger
    finally:
        srv.kill()
    # Assert
    assert decided is False


# ==========================================================================
# THE DOUBLE-RESTART: never probe during your own restart
# ==========================================================================


def _drive_to_restart(probe: _Probe) -> None:
    """Two refusals — enough to corroborate and issue restart #1.

    That this REALLY restarts is pinned by
    ``test_second_refusal_restarts_dead_listen``, so the tests below are not
    vacuous when they assert no FURTHER restart follows.
    """
    probe.run()
    probe.run()


def test_backoff_blocks_the_second_restart(tmp_path):
    # Arrange — THE incident. The old probe restarted, then re-probed 26s
    # later DURING its own restart, saw a genuinely-down daemon, and
    # restarted AGAIN. Drive it to a real restart, then keep probing a
    # still-dead port.
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    _drive_to_restart(probe)
    # Act — the timer keeps firing while the daemon is coming back up.
    followups = [probe.restarted() for _ in range(3)]
    # Assert — NOT ONE further restart inside the cooling-off window.
    assert followups == [False, False, False]


def test_backoff_refuses_to_even_probe(tmp_path):
    # Arrange
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    probe.run()
    probe.run()  # restart issued
    # Act
    result = probe.run()
    # Assert — you cannot judge a daemon you are in the middle of restarting.
    assert "post-restart backoff" in result.stderr and "NOT probing" in result.stderr


def test_backoff_expiry_allows_healing_again(tmp_path):
    # Arrange — the backoff must not be a permanent gag: once it lapses, a
    # still-dead listen is healed again.
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    probe.run()
    probe.run()  # restart #1
    # Act — a 0s backoff is the expiry, exactly as the timer would see it.
    probe.env["SAC_LISTEN_RESTART_BACKOFF"] = "0"
    probe.run()  # failure 2/3 again
    decided = probe.restarted()
    # Assert
    assert decided is True


# ==========================================================================
# RATE LIMIT — an unbounded restarter is how a fleet goes down at 3am
# ==========================================================================


def test_restarts_are_capped_per_window(tmp_path):
    # Arrange — a listen that never comes back. Backoff off so we can reach
    # the cap; the cap is the thing under test.
    probe = _Probe(
        tmp_path,
        f"http://127.0.0.1:{_closed_port()}/v1/health",
        SAC_LISTEN_RESTART_BACKOFF=0,
    )
    # Act — six probes; each pair corroborates into one restart attempt.
    decisions = [probe.restarted() for _ in range(6)]
    # Assert — exactly MAX_RESTARTS (2), then it stops. Not six.
    assert sum(decisions) == 2


@pytest.fixture()
def exhausted_probe(tmp_path):
    """A watchdog that has spent its restart budget on a listen still dead."""
    probe = _Probe(
        tmp_path,
        f"http://127.0.0.1:{_closed_port()}/v1/health",
        SAC_LISTEN_RESTART_BACKOFF=0,
    )
    for _ in range(5):  # 2 restarts issued; the cap (2) is now reached
        probe.run()
    return probe


def test_exhausted_watchdog_gives_up_loudly(exhausted_probe):
    # Arrange — the restart budget is spent and the listen is still dead.
    probe = exhausted_probe
    # Act
    result = probe.run()
    # Assert
    assert "GIVING UP" in result.stderr


def test_exhausted_watchdog_calls_for_a_human(exhausted_probe):
    # Arrange
    probe = exhausted_probe
    # Act
    result = probe.run()
    # Assert — if 2 restarts did not fix it, a 3rd will not either.
    assert "HUMAN IS NEEDED" in result.stderr


def test_exhausted_watchdog_emits_incident_class(exhausted_probe):
    # Arrange
    probe = exhausted_probe
    # Act
    result = probe.run()
    # Assert — greppable in the journal, like the autorestart alarm.
    assert "incident-class=sac-listen-watchdog-giving-up" in result.stderr


def test_giving_up_still_exits_nonzero(exhausted_probe):
    # Arrange — refusing to restart must NOT read as success: the outage is
    # ongoing and the exit code has to say so.
    probe = exhausted_probe
    # Act
    result = probe.run()
    # Assert
    assert result.returncode == 1


# ==========================================================================
# --check-only is a PROBE: it must not mutate anything
# ==========================================================================


def test_check_only_writes_no_state(tmp_path):
    # Arrange — a probe that mutates is not a probe.
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    # Act
    for _ in range(5):
        probe.run("--check-only")
    # Assert — five failed check-only probes leave NO ledger and NO restart.
    assert not probe.state.exists()


def test_check_only_never_restarts(tmp_path):
    # Arrange
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    # Act
    outs = [probe.run("--check-only").stderr for _ in range(5)]
    # Assert
    assert not any("RESTARTING" in o for o in outs)


# ==========================================================================
# The ledger is DATA, not code — and a corrupt one must fail SAFE
# ==========================================================================


def test_corrupt_ledger_does_not_restart(tmp_path):
    # Arrange — garbage (and a shell injection attempt) in the state file.
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    probe.state.write_text(
        "failures=$(touch /tmp/sac-pwned)\n"
        "serving_streak=../../etc\n"
        "last_restart=NaN\n"
        "restarts=x;rm -rf /\n"
    )
    # Act
    decided = probe.restarted()
    # Assert — unparseable == no history == do NOT restart. Fail safe.
    assert decided is False


def test_corrupt_ledger_is_not_executed(tmp_path):
    # Arrange — the state file must never be `source`d.
    canary = tmp_path / "pwned"
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    probe.state.write_text(f"failures=9\nrestarts=$(touch {canary})\n")
    # Act
    probe.run()
    # Assert
    assert not canary.exists()


def test_status_reports_the_ledger(tmp_path):
    # Arrange
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    probe.run()  # accrue a real failure
    # Act
    result = probe.run("--status")
    # Assert — an operator must be able to see WHY it is or is not acting.
    assert "failure_weight:  2 / 3" in result.stdout


def test_reset_clears_the_ledger(tmp_path):
    # Arrange
    probe = _Probe(tmp_path, f"http://127.0.0.1:{_closed_port()}/v1/health")
    probe.run()
    # Act
    probe.run("--reset")
    decided = probe.restarted()
    # Assert — after a reset, one failure is once again just one failure.
    assert decided is False


# ==========================================================================
# A daemon that DIES mid-life is still healed
# ==========================================================================


def test_daemon_that_dies_is_restarted(tmp_path):
    # Arrange — healthy, then genuinely gone. The 2026-06-26 shape.
    srv = _Server(status=200)
    probe = _Probe(tmp_path, srv.url)
    healthy = probe.restarted()
    # Act
    srv.kill()  # the port now really refuses
    first_after_death = probe.restarted()
    second_after_death = probe.restarted()
    # Assert — it stays quiet while healthy, and heals once death is
    # corroborated. Crash coverage is intact.
    assert (healthy, first_after_death, second_after_death) == (False, False, True)


def test_probe_script_has_valid_bash_syntax():
    # Arrange
    cmd = ["bash", "-n", str(_PROBE)]
    # Act
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Assert
    assert result.returncode == 0, result.stderr
