"""Test ports: one that REFUSES connections, and one you are about to bind.

Why this exists (CI failure 2026-08-12, develop ``d21cb5f6``, **py3.11 leg
only** — py3.12 and py3.13 green on the identical tree)::

    FAILED tests/integration/test_sac_listen_health_watchdog_decision.py
        ::test_one_success_does_not_wipe_failure_streak
    assert (False, False) == (False, True)

The test was right; its port helper was wrong. Ten-plus test modules had each
hand-rolled the same idiom to mean "a port nothing is listening on" —

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()          # <-- the port is RELEASED here
    return port

— and that ``close()`` is a **race by construction**. The instant it returns,
the port is free again and back in the kernel's ephemeral pool. Between that
line and the moment the test actually probes the URL, *anything* can take it:
another test in the same session, another xdist worker (CI runs ``-n``), any
process on the box. When something does, the URL the test believes is DEAD
**answers**, no failure accrues, and a streak assertion inverts. It is
per-leg nondeterministic rather than a clean pass/fail, which is exactly the
signature we saw: one leg red, two green, same commit.

The property the failing test guards is a real shipped bug (#673): one lucky
reply reset ``consecutive_unhealthy``, so a flapping daemon oscillated
``1/2 -> "healthy" -> 1/2`` forever and was never acted on. Relaxing that
assertion — or papering over the race with a retry or a sleep — would delete
the regression guard and keep the flake. So the helper is what changes.

**The fix: bind, and never close.** A socket that is ``bind()``ed but never
``listen()``ed gives both properties at once, and neither is an accident of
timing:

* **It refuses.** With no accept queue the kernel answers the SYN with RST,
  so ``connect()`` fails ``ECONNREFUSED`` and curl exits 7 with "Failed to
  connect". That is precisely the "nothing is listening here" semantics the
  callers want — the same thing they were trying to synthesise by closing.
* **It holds the port.** While the socket lives nothing else can bind that
  port, and the kernel will not hand it out to another ``bind(port 0)``.

Measured on this platform (see ``test_ports_helper.py``, which asserts every
one of these rather than trusting the paragraph above): connect ->
``ECONNREFUSED`` (errno 111); curl -> exit 7; a second ``bind()`` -> errno 98
``EADDRINUSE``, and still ``EADDRINUSE`` when the newcomer sets
``SO_REUSEADDR`` **or** ``SO_REUSEPORT``, and still ``EADDRINUSE`` when the
newcomer asks for the wildcard ``0.0.0.0``. By contrast the old
bind-then-close port re-binds on the first try — that *is* the bug, and it is
pinned as a test too, so this module's value is falsifiable rather than
asserted.

Deliberately, the holder socket sets **no** socket options. ``SO_REUSEADDR``
and ``SO_REUSEPORT`` are the two things that would let a later binder share
the port; setting either on the holder would re-open the hole this module
closes.

**Why this defect is durable, and what that demands of this file.** The bug
survives review because ``s.close()`` *looks* deliberate — in scitex-dev's
copy it is even commented ``# Socket is closed by here, which is the point:
the port is free.`` A reader sees an obviously-intentional close and moves
on. The un-close must therefore be at least as obviously intentional, or the
next reader will "tidy up the leaked socket" and restore the flake. That is
why the not-closing is stated at every level here: in this docstring, in a
comment at the ``bind()`` itself, and — the part that actually bites — in
``test_ports.py::test_helper_socket_is_still_open_after_handing_out_port``,
which fails if someone adds the close back.

**Promoting this to scitex-dev.** The same defect exists at
``scitex-dev tests/scitex_dev/_cli/gui/test__lifecycle.py::free_port``, whose
docstring promises "a port number nothing is listening on" while the code
only guarantees "was free a moment ago" — so this module is written to move
there (scitex-dev hosts the primitive, leaves declare specifics) rather than
be welded to sac. It imports only ``contextlib``, ``socket``, ``typing`` and
``pytest``; nothing from ``scitex_agent_container``, nothing from the sac
fleet, no repo-relative paths, no env vars. A promotion would change three
things and nothing else: the module's home, the ``from tests...._helpers.ports
import`` lines at each call site, and the re-export in ``tests/conftest.py``
that makes ``dead_port`` a suite-wide fixture. Prove it in the leaf first —
this PR — then promote.

**The other need, which is NOT this one.** Some call sites reach for the same
idiom to mean "a free port I am about to bind MYSELF" — pick one, then hand it
to uvicorn, ``http.server``, or a subprocess. That need genuinely requires
releasing the port, so it cannot use ``dead_port``, and it is racy for a
different reason (someone else may take the port before the real server binds
it). ``reserved_port`` serves it honestly: it holds the port right up until
the caller is ready, shrinking the window to the smallest one a
non-fd-passing API allows, and it says so out loud rather than pretending the
race is gone.

NO MOCKS — real sockets, real kernel behaviour, real refusals.
"""

from __future__ import annotations

import contextlib
import socket
from typing import Iterator, List

import pytest

__all__ = [
    "DeadPortAllocator",
    "bind_without_listen",
    "dead_port",
    "hold_dead_port",
    "reserved_port",
]

_LOOPBACK = "127.0.0.1"


def bind_without_listen(host: str = _LOOPBACK) -> socket.socket:
    """A socket BOUND to an ephemeral port and never ``listen()``ed.

    Connecting to it is REFUSED (no accept queue -> RST), and the port is
    HELD for as long as the returned socket is open. See the module docstring
    for why both halves matter and how they are measured.

    The caller owns the socket and must close it. Prefer ``hold_dead_port``
    (a context manager) or the ``dead_port`` fixture, which do that for you.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # No SO_REUSEADDR / SO_REUSEPORT on purpose: those are exactly what would
    # let another binder move in behind the test's back.
    sock.bind((host, 0))
    # !! DO NOT close() OR listen() THIS SOCKET HERE. !!
    # Returning it OPEN is the entire point, not a leaked fd:
    #   * open + never listen()ed  ->  connections are REFUSED (what callers want)
    #   * open                     ->  the port cannot be stolen (what fixes the flake)
    # Closing here restores the 2026-08-12 py3.11 flake exactly. Ownership
    # passes to the caller; hold_dead_port / the dead_port fixture close it at
    # teardown. Pinned by test_ports.py::
    #   test_helper_socket_is_still_open_after_handing_out_port
    return sock


class DeadPortAllocator:
    """Hands out refusing ports and HOLDS every one of them until ``close()``.

    Callable, so a call site that had ``_closed_port()`` inline in an f-string
    keeps its shape::

        def test_refused_is_reported_as_down(tmp_path, dead_port):
            probe = _Probe(tmp_path, dead_port.url("/v1/health"))
    """

    def __init__(self, host: str = _LOOPBACK) -> None:
        self._host = host
        self._held: List[socket.socket] = []

    def __call__(self) -> int:
        """A fresh port that refuses connections, held until teardown."""
        sock = bind_without_listen(self._host)
        self._held.append(sock)
        return int(sock.getsockname()[1])

    def url(self, path: str = "/", scheme: str = "http") -> str:
        """A URL on a fresh dead port — the shape most call sites want."""
        return f"{scheme}://{self._host}:{self()}{path}"

    def close(self) -> None:
        """Release every port handed out. Idempotent."""
        for sock in self._held:
            with contextlib.suppress(OSError):
                sock.close()
        self._held.clear()


@contextlib.contextmanager
def hold_dead_port(host: str = _LOOPBACK) -> Iterator[int]:
    """One port that refuses connections, held for the duration of the block.

    For code that is not a pytest test (module-level smoke helpers, say) and
    therefore cannot take the ``dead_port`` fixture.
    """
    sock = bind_without_listen(host)
    try:
        yield int(sock.getsockname()[1])
    finally:
        sock.close()


@contextlib.contextmanager
def reserved_port(host: str = _LOOPBACK) -> Iterator[socket.socket]:
    """A free port HELD until you are ready to bind it yourself.

    The other need (see the module docstring): the caller is about to start a
    REAL server — uvicorn, ``http.server``, a subprocess — on this port, so
    the port must actually be free when that server binds. This cannot be
    ``dead_port``; releasing is the point.

    It is still an improvement on bind-then-close-then-return-an-int, because
    the port stays held for the whole setup in between (writing a spec file,
    building a config, spawning a process) instead of being released at the
    top. Yield gives you the SOCKET; call ``.close()`` on it at the last
    moment, immediately before the real bind::

        with reserved_port() as sock:
            port = sock.getsockname()[1]
            config = build_config(port=port)   # port still held here
            sock.close()                       # release
            serve(config)                      # ... and immediately re-bind

    Be honest about what remains: between that ``close()`` and the server's
    ``bind()`` the port IS free, and no helper can shrink that to zero without
    passing the file descriptor to the server (which most of these APIs do not
    accept). If a server DOES accept an fd, pass it this socket instead and
    the race disappears entirely.
    """
    sock = bind_without_listen(host)
    try:
        yield sock
    finally:
        with contextlib.suppress(OSError):
            sock.close()


@pytest.fixture()
def dead_port() -> Iterator[DeadPortAllocator]:
    """Call it for a port that REFUSES connections and cannot be stolen.

    ``dead_port()`` -> int, ``dead_port.url("/v1/health")`` -> str. Every port
    handed out stays bound (and therefore refusing, and therefore un-stealable)
    until this test finishes.
    """
    allocator = DeadPortAllocator()
    try:
        yield allocator
    finally:
        allocator.close()
