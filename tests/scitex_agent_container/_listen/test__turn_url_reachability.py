"""Regression tests: a derived local ``turn_url`` must actually CONNECT.

The bug (deterministic, 100% reproducible — reported by
``claude-code-telegrammer``, independently re-measured on this box)::

    _listen/_registry_endpoints.py::derive_turn_url()
        builds  http://<canonical-hostname>:<port>/v1/turn

    $ getent hosts ywata-note-win
    127.0.1.1   ywata-note-win.localdomain ywata-note-win   <-- .1.1, not .0.1

    127.0.0.1:19017        -> OPEN
    ywata-note-win:19017   -> CONNECTION REFUSED

The a2a sidecar binds ``127.0.0.1`` (``a2a/_server.py``: ``host: str =
"127.0.0.1"``), but the machine's own hostname resolves to ``127.0.1.1``
— the stock Debian / Ubuntu / WSL self-host convention. A different
loopback address means REFUSED, every time, for every local consumer of
the derived URL. Not flaky: deterministic.

These tests bind a REAL socket on ``127.0.0.1`` exactly as the sidecar
does, then assert the URL ``derive_turn_url`` hands out actually connects
to it. A URL that "looks right" but refuses is precisely the failure mode
being closed, so the only honest assertion is a real ``connect()`` — a
string-shape assertion would have passed happily throughout the outage.

The predicate underneath is covered in ``test__local_host.py``.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlsplit

from scitex_agent_container._listen import _registry_endpoints as re_mod
from scitex_agent_container._listen._local_host import LOOPBACK_HOST


@contextmanager
def _sidecar() -> Iterator[int]:
    """Bind a REAL listening socket on 127.0.0.1, exactly like a2a/_server.

    Yields the ephemeral port. This is the thing a ``turn_url`` is
    supposed to reach; if the derived URL cannot connect to it, the URL
    is wrong.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LOOPBACK_HOST, 0))
    sock.listen(8)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _connects(url: str) -> bool:
    """True iff the URL's host:port accepts a TCP connection RIGHT NOW."""
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=2.0):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The reported bug — the derived URL must be reachable
# ---------------------------------------------------------------------------


def test_debian_self_host_address_derives_reachable_url() -> None:
    # Arrange — 127.0.1.1 is what a Debian/Ubuntu/WSL box's own hostname
    # resolves to. It IS a loopback address, but NOT the one the sidecar
    # binds, so a URL naming it is refused on every single connection.
    with _sidecar() as port:
        # Act
        url = re_mod.derive_turn_url("127.0.1.1", port)
        reachable = _connects(url)
    # Assert
    assert reachable, (
        "derive_turn_url handed out a URL that cannot connect to the sidecar "
        "— 127.0.1.1 is a DIFFERENT loopback address from the 127.0.0.1 the "
        "a2a sidecar binds"
    )


def test_own_hostname_derives_reachable_url() -> None:
    # Arrange — the actual field path: resolve_a2a_host() returns this
    # machine's canonical hostname for a LOCAL agent and derive_turn_url
    # used it verbatim. On any box following the Debian self-host
    # convention that URL is refused 100% of the time.
    with _sidecar() as port:
        # Act
        url = re_mod.derive_turn_url(socket.gethostname(), port)
        reachable = _connects(url)
    # Assert
    assert reachable, (
        f"the turn_url derived for a LOCAL agent from this machine's own "
        f"hostname ({socket.gethostname()!r}) does not connect to the sidecar "
        f"bound on {LOOPBACK_HOST}"
    )


def test_localhost_derives_reachable_url() -> None:
    # Arrange — "localhost" can resolve to ::1 first; the sidecar is IPv4
    # loopback only, so it must be normalised too.
    with _sidecar() as port:
        # Act
        url = re_mod.derive_turn_url("localhost", port)
        reachable = _connects(url)
    # Assert
    assert reachable


def test_local_host_normalises_to_loopback_literal() -> None:
    # Arrange — the derived URL for a local agent must name the exact
    # address the sidecar binds, not a name that merely resolves nearby.
    hostname = socket.gethostname()
    # Act
    url = re_mod.derive_turn_url(hostname, 19017)
    # Assert
    assert url == f"http://{LOOPBACK_HOST}:19017/v1/turn"


# ---------------------------------------------------------------------------
# Cross-host callers must keep the hostname form
# ---------------------------------------------------------------------------


def test_remote_host_keeps_its_hostname() -> None:
    # Arrange — a genuinely REMOTE peer is not on our loopback. Rewriting
    # its URL to 127.0.0.1 would point every cross-host dispatch at
    # ourselves — a far worse bug than the one being fixed.
    # Act
    url = re_mod.derive_turn_url("spartan-cpu.example.org", 19017)
    # Assert
    assert url == "http://spartan-cpu.example.org:19017/v1/turn"


def test_remote_ip_keeps_its_address() -> None:
    # Arrange — a routable (non-loopback) IP is a remote peer.
    # Act
    url = re_mod.derive_turn_url("100.64.1.2", 7878)
    # Assert
    assert url == "http://100.64.1.2:7878/v1/turn"


# ---------------------------------------------------------------------------
# Pre-existing contract must not regress
# ---------------------------------------------------------------------------


def test_missing_port_still_returns_none() -> None:
    # Arrange — the caller branches on ``turn_url is None`` to skip dispatch.
    host = socket.gethostname()
    # Act
    url = re_mod.derive_turn_url(host, None)
    # Assert
    assert url is None


def test_missing_host_still_returns_none() -> None:
    # Arrange — an unresolvable host must NOT silently become loopback:
    # that would advertise OUR sidecar as some other agent's endpoint.
    # Act
    url = re_mod.derive_turn_url(None, 19017)
    # Assert
    assert url is None


def test_empty_host_still_returns_none() -> None:
    # Arrange — same reasoning for the empty-string case.
    # Act
    url = re_mod.derive_turn_url("", 19017)
    # Assert
    assert url is None
