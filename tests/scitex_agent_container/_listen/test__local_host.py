"""Tests for ``_listen/_local_host.py`` — "does this hostname mean THIS box?"

The predicate that fixes a deterministic outage. ``derive_turn_url``
named the machine's own canonical hostname, which on stock Debian /
Ubuntu / WSL resolves to ``127.0.1.1``::

    $ getent hosts ywata-note-win
    127.0.1.1   ywata-note-win.localdomain ywata-note-win

…while the a2a sidecar binds ``127.0.0.1``. Both are loopback, but they
are DIFFERENT loopback addresses, so every connection to the derived URL
was refused — 100% of the time.

The trap this pins: a naive ``host == "127.0.0.1"`` check MISSES
``127.0.1.1`` and happily emits the dead URL. "Local" has to mean *any*
loopback address, all of which normalise to the one the sidecar binds.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import socket

import pytest

from scitex_agent_container._listen._local_host import (
    LOOPBACK_HOST,
    is_local_host,
    local_host_aliases,
)


# ---------------------------------------------------------------------------
# The reported trap — 127.0.1.1 is loopback, and is NOT 127.0.0.1
# ---------------------------------------------------------------------------


def test_debian_self_host_ip_is_local() -> None:
    # Arrange — the crux of the bug. 127.0.1.1 is a loopback address, so a
    # naive equality check against 127.0.0.1 would miss it and emit a URL
    # that is refused on every single connection.
    # Act
    local = is_local_host("127.0.1.1")
    # Assert
    assert local


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.1.2.3", "::1"])
def test_loopback_addresses_are_local(host: str) -> None:
    # Arrange — everything in 127.0.0.0/8 (and ::1) is reachable only from
    # this machine, so all of it is "local" and all of it must normalise.
    # Act
    local = is_local_host(host)
    # Assert
    assert local


# ---------------------------------------------------------------------------
# This machine's own names
# ---------------------------------------------------------------------------


def test_own_hostname_is_local() -> None:
    # Arrange — this is the name ``resolve_a2a_host`` hands to
    # ``derive_turn_url`` for a LOCAL agent.
    # Act
    local = is_local_host(socket.gethostname())
    # Assert
    assert local


def test_short_hostname_is_local() -> None:
    # Arrange — an ``instances`` row may carry the short label rather than
    # the FQDN; both denote this machine.
    short = socket.gethostname().split(".", 1)[0]
    # Act
    local = is_local_host(short)
    # Assert
    assert local


def test_hostname_match_is_case_insensitive() -> None:
    # Arrange — DNS names are case-insensitive; a row carrying an
    # upper-cased hostname must not slip through as "remote".
    # Act
    local = is_local_host(socket.gethostname().upper())
    # Assert
    assert local


@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "::", ""])
def test_always_local_literals_are_local(host: str) -> None:
    # Arrange — a wildcard listener IS reachable on loopback, and
    # "localhost" can resolve to ::1 first, so both are normalised.
    # Act
    local = is_local_host(host)
    # Assert
    assert local


# ---------------------------------------------------------------------------
# Remote peers must NOT be normalised (that would point every cross-host
# dispatch back at ourselves)
# ---------------------------------------------------------------------------


def test_remote_hostname_is_not_local() -> None:
    # Arrange
    # Act
    local = is_local_host("spartan-cpu.example.org")
    # Assert
    assert not local


def test_routable_ip_is_not_local() -> None:
    # Arrange — a tailscale / VPN address belongs to another box.
    # Act
    local = is_local_host("100.64.1.2")
    # Assert
    assert not local


def test_none_host_is_not_local() -> None:
    # Arrange — a missing host is not a claim that it is ours. Promoting
    # it to loopback would advertise OUR sidecar as somebody else's.
    # Act
    local = is_local_host(None)
    # Assert
    assert not local


# ---------------------------------------------------------------------------
# local_host_aliases + the loopback constant
# ---------------------------------------------------------------------------


def test_aliases_include_the_short_hostname() -> None:
    # Arrange
    short = socket.gethostname().split(".", 1)[0].lower()
    # Act
    aliases = local_host_aliases()
    # Assert
    assert short in aliases


def test_aliases_are_lower_cased() -> None:
    # Arrange — the predicate lower-cases its input, so the alias set must
    # be lower-cased too or the comparison silently never matches.
    # Act
    aliases = local_host_aliases()
    # Assert
    assert all(alias == alias.lower() for alias in aliases)


def test_loopback_host_is_the_sidecar_bind_address() -> None:
    # Arrange — a2a/_server.py binds ``host: str = "127.0.0.1"``. If these
    # two ever drift, every derived local turn_url dies again.
    # Act
    address = LOOPBACK_HOST
    # Assert
    assert address == "127.0.0.1"


def test_injected_aliases_override_the_real_hostname() -> None:
    # Arrange — the predicate is injectable so derivation can be tested
    # without depending on the runner's /etc/hosts.
    # Act
    local = is_local_host("some-box", aliases=frozenset({"some-box"}))
    # Assert
    assert local
