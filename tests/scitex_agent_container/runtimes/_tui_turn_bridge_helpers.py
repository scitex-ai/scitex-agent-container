"""Shared helpers and the real-server fixture for the TUI turn-bridge tests."""

from __future__ import annotations

import json
import shutil
import socket
import time
import urllib.error
import urllib.request
from types import SimpleNamespace


from scitex_agent_container.runtimes import _tui_turn_bridge as bridge

from ._tui_turn_bridge_fixtures import (
    bridge_factory as bridge_factory,
    isolated_home as isolated_home,
)

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
# HTTP server — real ThreadingHTTPServer on an ephemeral port
# ---------------------------------------------------------------------------


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


def _wait_exchange(
    port: int,
    exchange_id: str,
    *,
    pending: tuple[int, ...] = (202,),
    timeout_s: float = 3.0,
) -> dict:
    # Returns the first read whose code is not in ``pending``. Bounded by
    # WALL TIME, not an iteration count: under 32 xdist workers on the org CI
    # runner each GET can take far longer than the sleep, so an iteration
    # budget expired early (measured 2026-10-16). A healthy run returns on
    # the first terminal read; only a genuinely stuck exchange waits.
    # Callers waiting for a FINAL outcome pass ``pending=(202, 102)``: 102 is
    # "still retrying", and returning it raced the background visibility
    # retry (test_visible_delivery_retries_one_exchange_then_finishes_without
    # _resubmit read 102 when its third native call had not yet run).
    deadline = time.monotonic() + timeout_s
    while True:
        _status, body = _get(port, f"/v1/exchanges/{exchange_id}")
        if body["status_code"]["code"] not in pending:
            return body
        if time.monotonic() >= deadline:
            return body
        time.sleep(0.001)


def _wait_delivery_idle(
    server, calls: dict, *, attempts: int, timeout_s: float = 10.0
) -> None:
    """Wait until ``attempts`` native calls ran and the bridge released them.

    A retry POST that lands while the previous attempt still holds
    ``active_delivery`` is answered "already in progress" (202) and starts no
    new attempt; that is the bridge's contract. The exchange row reads 102 both
    before and after a retry attempt, so polling the row cannot tell when
    the next POST will be admitted. Only the released in-flight slot can.
    """
    deadline = time.monotonic() + timeout_s
    while calls["count"] < attempts or server.active_delivery is not None:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"bridge not idle after {attempts} attempt(s): "
                f"count={calls['count']} active={server.active_delivery!r}"
            )
        time.sleep(0.001)


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
