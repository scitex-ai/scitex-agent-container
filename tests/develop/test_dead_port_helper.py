"""The dead-port helper must be FALSIFIABLE, so every claim it makes is a test.

WHY THIS FILE LIVES IN ``tests/develop/`` AND NOT BESIDE THE HELPER
==================================================================
It tests ``tests/scitex_agent_container/_helpers/ports.py``, so the obvious
home is next to it. That home is wrong, and the auditor is right to say so.

``tests/<pkg>/`` is the MIRROR TREE: every ``test_X.py`` there must
correspond to ``src/<pkg>/.../X.py``. **PS-204 §2 (orphan-test-file)** fires
on any that does not, and this test has no src counterpart *by design* — the
thing under test is test-support code that must never ship. Putting it in the
mirror tree reddened both matrix legs (PR #1026, run 31607851531): audit-all
exit 1, 1 unmasked error, green on the base commit.

The helper itself stays exactly where it is. It is not a ``test_*.py``, so
PS-204 never looks at it, and **PS-207 is documented as "src-aware so it
never flags fixture trees that legitimately have no source counterpart"** —
i.e. a `_helpers/` directory of support modules with no `src/` mirror is a
BLESSED shape, which is why `loopback_server.py` and `ssh_exec_shim.py` have
sat there unflagged. Only the *test* had to move, and relocating is the
remedy PS-204 itself is built to suggest (its detail is "enriched" precisely
to name a relocate target), not a dodge around the rule.

``tests/develop/`` is where this repo keeps gates about its own tooling and
conventions — ``test_audit.py``, ``test_git_hooks.py``,
``test_pytest_plugin_gate.py``, ``test_skills_quality.py``. A gate pinning
the contract of shared test infrastructure is exactly that genre, and the
directory is outside PS-204's scope. Do not move this back.


A test-support module that nothing tests is an assertion, not a guarantee —
and the bug this one fixes (CI 2026-08-12, develop ``d21cb5f6``, py3.11 leg
only) is precisely a helper that *claimed* "nothing is listening on this port"
and delivered "nothing was listening on this port a moment ago". A test which
would ALSO pass against the broken bind-then-close helper proves nothing, so
each property below is paired with the old idiom failing it.

Two claims, both measured here rather than asserted in prose:

* a port from ``dead_port`` REFUSES connections (``ECONNREFUSED``, and curl
  exits 7 — the real client the shipped watchdog probe uses);
* that port is HELD — nothing can bind it, not with ``SO_REUSEADDR``, not
  with ``SO_REUSEPORT``, not via the wildcard address, and not by asking the
  kernel for an ephemeral port.

Plus the anti-regression one that matters most in practice: the helper's
socket is still OPEN after it hands out the port. The defect is durable
because ``close()`` looks intentional; this is what fails when a future
reader "tidies up the leaked socket" and restores the flake.

NO MOCKS — real sockets, real binds, a real curl.

AAA markers (TQ002); 3+-word test names.
"""

from __future__ import annotations

import errno
import shutil
import socket
import subprocess
import urllib.request

import pytest

from tests.scitex_agent_container._helpers.loopback_server import run_loopback
from tests.scitex_agent_container._helpers.ports import (
    DeadPortAllocator,
    bind_without_listen,
    hold_dead_port,
    reserved_port,
)


def _old_broken_closed_port() -> int:
    """The idiom being replaced, verbatim, so the new tests are falsifiable.

    Kept HERE (never exported) purely as the negative control: several tests
    below assert that this shape fails the property the new helper passes. If
    a test cannot tell the two apart, it is not testing anything.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _connect_error(port: int, timeout: float = 2.0) -> OSError | None:
    """Really connect. Return the OSError, or None if the connection SUCCEEDED."""
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(("127.0.0.1", port))
        return None
    except OSError as exc:
        return exc
    finally:
        client.close()


def _bind_error(port: int, *, opts: tuple = (), host: str = "127.0.0.1") -> OSError | None:
    """Really try to bind `port`. Return the OSError, or None if the bind WON."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        for opt in opts:
            sock.setsockopt(socket.SOL_SOCKET, opt, 1)
        sock.bind((host, port))
        return None
    except OSError as exc:
        return exc
    finally:
        sock.close()


# ==========================================================================
# CLAIM 1 — a dead port REFUSES connections
# ==========================================================================


def test_dead_port_refuses_connections():
    # Arrange
    with hold_dead_port() as port:
        # Act
        err = _connect_error(port)
    # Assert — a bound-but-never-listen()ed socket has no accept queue, so the
    # kernel answers the SYN with RST. That IS "nothing is listening here".
    assert err is not None and err.errno == errno.ECONNREFUSED


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs a real curl")
def test_dead_port_refuses_real_curl():
    # Arrange — curl is the client the SHIPPED watchdog probe actually uses,
    # so its verdict is the one that decides the test this fix unbreaks.
    with hold_dead_port() as port:
        cmd = ["curl", "-sS", "-m", "5", f"http://127.0.0.1:{port}/v1/health"]
        # Act
        result = subprocess.run(cmd, capture_output=True, text=True)
    # Assert — curl(7) is "Failed to connect", i.e. connection refused.
    assert result.returncode == 7, result.stderr


# ==========================================================================
# CLAIM 2 — a dead port is HELD, and the old idiom's is not
# ==========================================================================


def test_dead_port_cannot_be_rebound():
    # Arrange
    with hold_dead_port() as port:
        # Act
        err = _bind_error(port)
    # Assert
    assert err is not None and err.errno == errno.EADDRINUSE


def test_released_port_can_be_rebound():
    # Arrange — the negative control: THE BUG. If this ever starts failing,
    # the tests above have stopped discriminating and must be re-examined.
    port = _old_broken_closed_port()
    # Act
    err = _bind_error(port)
    # Assert — the "dead" port is free for anyone to take, which is exactly
    # how a URL the test believed was dead ends up ANSWERING.
    assert err is None


def test_reuseaddr_cannot_steal_dead_port():
    # Arrange — http.server and uvicorn both set SO_REUSEADDR, so a competing
    # server in the same suite is the realistic thief.
    with hold_dead_port() as port:
        # Act
        err = _bind_error(port, opts=(socket.SO_REUSEADDR,))
    # Assert — SO_REUSEADDR only helps against TIME_WAIT, not a live bind.
    assert err is not None and err.errno == errno.EADDRINUSE


def test_reuseport_cannot_steal_dead_port():
    # Arrange — SO_REUSEPORT shares a port only when EVERY binder sets it.
    # The holder deliberately sets no options, so it can never be shared.
    with hold_dead_port() as port:
        # Act
        err = _bind_error(port, opts=(socket.SO_REUSEPORT,))
    # Assert
    assert err is not None and err.errno == errno.EADDRINUSE


def test_wildcard_bind_cannot_steal_dead_port():
    # Arrange — a server told to listen on 0.0.0.0 must not be able to take a
    # port held on 127.0.0.1 out from under the test.
    with hold_dead_port() as port:
        # Act
        err = _bind_error(port, opts=(socket.SO_REUSEADDR,), host="0.0.0.0")
    # Assert
    assert err is not None and err.errno == errno.EADDRINUSE


def test_ephemeral_allocation_skips_held_port():
    # Arrange — the xdist-worker case: another test asks the kernel for "any
    # free port". It must never be handed one we are holding.
    with hold_dead_port() as held:
        others = []
        try:
            # Act — enough draws that a colliding allocator would show up.
            for _ in range(50):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind(("127.0.0.1", 0))
                others.append(sock)
            drawn = [s.getsockname()[1] for s in others]
        finally:
            for sock in others:
                sock.close()
    # Assert
    assert held not in drawn


# ==========================================================================
# THE ANTI-REGRESSION — the close must stay gone
# ==========================================================================


@pytest.fixture()
def raw_holder():
    """The socket ``bind_without_listen`` returns, closed after the test."""
    sock = bind_without_listen()
    try:
        yield sock
    finally:
        sock.close()


def test_helper_socket_is_still_open_after_handing_out_port(raw_holder):
    # Arrange — THIS is the test that fires when someone "fixes the leaked
    # socket". Returning it OPEN is the fix, not an oversight.
    # Act — fileno() is -1 on a closed socket; a live one answers its port.
    still_open = raw_holder.fileno() != -1
    # Assert
    assert still_open


def test_open_helper_socket_still_owns_its_port(raw_holder):
    # Arrange
    port = raw_holder.getsockname()[1]
    # Act
    err = _bind_error(port)
    # Assert — being open is only useful because it keeps the port.
    assert err is not None and err.errno == errno.EADDRINUSE


def test_bound_helper_socket_is_not_listening(raw_holder):
    # Arrange — if anyone adds listen(), the port stops refusing and every
    # "dead endpoint" test silently starts probing a live socket.
    # Act
    err = _connect_error(raw_holder.getsockname()[1])
    # Assert
    assert err is not None and err.errno == errno.ECONNREFUSED


# ==========================================================================
# THE ALLOCATOR / FIXTURE — many ports, all held, all released at teardown
# ==========================================================================


@pytest.fixture()
def allocator():
    """A ``DeadPortAllocator``, released after the test."""
    alloc = DeadPortAllocator()
    try:
        yield alloc
    finally:
        alloc.close()


def test_allocator_hands_out_distinct_ports(allocator):
    # Arrange — one test can need several dead ports at once.
    # Act
    ports = [allocator() for _ in range(5)]
    # Assert
    assert len(set(ports)) == 5


def test_allocator_holds_every_port_it_hands_out(allocator):
    # Arrange — holding only the LAST one would leave the earlier ports
    # stealable, which is the original bug with extra steps.
    ports = [allocator() for _ in range(5)]
    # Act
    errs = [_bind_error(p) for p in ports]
    # Assert
    assert all(e is not None and e.errno == errno.EADDRINUSE for e in errs)


def test_allocator_releases_ports_on_close():
    # Arrange — held for the test, not for the session: a helper that never
    # released would leak an fd per call across a 10k-test suite.
    allocator = DeadPortAllocator()
    ports = [allocator() for _ in range(3)]
    # Act
    allocator.close()
    # Assert
    assert all(_bind_error(p) is None for p in ports)


def test_allocator_close_is_idempotent():
    # Arrange
    allocator = DeadPortAllocator()
    allocator()
    # Act
    allocator.close()
    allocator.close()
    # Assert — a fixture teardown after an explicit close must not explode.
    assert allocator._held == []


def _port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1].split("/")[0])


def test_allocator_url_has_loopback_host_and_path(allocator):
    # Arrange
    # Act
    url = allocator.url("/v1/health")
    # Assert
    assert url.startswith("http://127.0.0.1:") and url.endswith("/v1/health")


def test_allocator_url_port_refuses_connections(allocator):
    # Arrange — the convenience form must be as dead as the raw one.
    url = allocator.url("/v1/health")
    # Act
    err = _connect_error(_port_of(url))
    # Assert
    assert err is not None and err.errno == errno.ECONNREFUSED


def test_allocator_url_port_is_held(allocator):
    # Arrange
    url = allocator.url("/v1/health")
    # Act
    err = _bind_error(_port_of(url))
    # Assert
    assert err is not None and err.errno == errno.EADDRINUSE


def test_dead_port_fixture_refuses_connections(dead_port):
    # Arrange — the suite-wide fixture, wired in tests/conftest.py.
    port = dead_port()
    # Act
    err = _connect_error(port)
    # Assert
    assert err is not None and err.errno == errno.ECONNREFUSED


def test_dead_port_fixture_holds_the_port(dead_port):
    # Arrange
    port = dead_port()
    # Act
    err = _bind_error(port)
    # Assert
    assert err is not None and err.errno == errno.EADDRINUSE


def test_dead_port_fixture_hands_out_distinct_ports(dead_port):
    # Arrange
    # Act
    ports = [dead_port() for _ in range(4)]
    # Assert
    assert len(set(ports)) == 4


def test_dead_port_fixture_holds_every_port(dead_port):
    # Arrange — not just the newest one.
    ports = [dead_port() for _ in range(4)]
    # Act
    errs = [_bind_error(p) for p in ports]
    # Assert
    assert all(e is not None and e.errno == errno.EADDRINUSE for e in errs)


# ==========================================================================
# THE OTHER NEED — a port you are about to bind YOURSELF
# ==========================================================================


def test_reserved_port_is_held_inside_the_block():
    # Arrange — the whole value of reserved_port over bind-then-close is that
    # the port survives the setup work in between.
    with reserved_port() as sock:
        port = sock.getsockname()[1]
        # Act
        err = _bind_error(port)
        # Assert
        assert err is not None and err.errno == errno.EADDRINUSE


def test_reserved_port_is_bindable_after_release():
    # Arrange — and it must genuinely hand the port over, or the real server
    # it was reserved for could never start.
    with reserved_port() as sock:
        port = sock.getsockname()[1]
        # Act
        sock.close()
        err = _bind_error(port)
    # Assert
    assert err is None


def test_reserved_port_close_inside_block_is_safe():
    # Arrange — the documented usage closes early; teardown must tolerate it.
    with reserved_port() as sock:
        port = sock.getsockname()[1]
        sock.close()
    # Act
    err = _bind_error(port)
    # Assert — no double-close explosion on the way out of the block.
    assert err is None


# ---------------------------------------------------------------------------
# run_loopback(sock=...) — closing the window rather than narrowing it.
#
# reserved_port is honest that it cannot get to zero on its own: "between that
# close() and the server's bind() the port IS free ... If a server DOES accept
# an fd, pass it this socket instead and the race disappears entirely."
# uvicorn.Config accepts fd=, so these assert the entirely.
#
# This is not theoretical. The gap fired on develop 2026-08-23 (py3.12,
# [Errno 98] on a port the loopback helper had just been handed) and, per
# _helpers/ports.py, on 2026-08-12 against py3.11. Which leg goes red is
# whichever worker loses the race, which is why the same bug looks like a
# different bug each time.
# ---------------------------------------------------------------------------


async def _no_content_app(scope, receive, send) -> None:
    """Smallest ASGI app that proves a real server bound and answered."""
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_run_loopback_serves_on_the_very_port_that_was_reserved():
    # Arrange — the whole point: the port the server ends up on must be the
    # port that was HELD, with no interval in which anyone could take it.
    with reserved_port() as sock:
        reserved = int(sock.getsockname()[1])
        # Act
        with run_loopback(_no_content_app, sock=sock) as served:
            pass
    # Assert
    assert served == reserved


def test_run_loopback_with_a_socket_actually_answers():
    # Arrange — equality of port numbers would also hold if the server never
    # started, so bind a real request to it. This is the falsifying half.
    with reserved_port() as sock:
        port = int(sock.getsockname()[1])
        # Act
        with run_loopback(_no_content_app, sock=sock):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=10
            ) as resp:
                status = resp.status
    # Assert
    assert status == 204


def test_run_loopback_refuses_a_port_and_a_socket_together():
    # Arrange — two sources for one value is how a test ends up asserting
    # against a port the server is not on.
    sock = bind_without_listen()
    port = int(sock.getsockname()[1])
    # Act
    def _both() -> None:
        with run_loopback(_no_content_app, port, sock=sock):
            pass
    # Assert
    with pytest.raises(TypeError):
        _both()
    sock.close()


def test_run_loopback_requires_either_a_port_or_a_socket():
    # Arrange — omitting both used to be a TypeError from the signature; it
    # must stay an error now that port is optional.
    app = _no_content_app
    # Act
    def _neither() -> None:
        with run_loopback(app):
            pass
    # Assert
    with pytest.raises(TypeError):
        _neither()


def test_old_idiom_hands_over_a_port_anyone_can_take():
    # Arrange — the negative control for the four above. If this ever stops
    # passing, the old idiom has been fixed elsewhere and these tests have
    # stopped discriminating between the two shapes.
    port = _old_broken_closed_port()
    # Act
    err = _bind_error(port)
    # Assert — free for the taking, which is the bug the fd path removes.
    assert err is None
