"""Resolve "does this hostname mean THIS machine?" — and if so, use loopback.

The bug this closes (deterministic, 100% reproducible)
======================================================
:func:`~._registry_endpoints.derive_turn_url` built an agent's
``turn_url`` as ``http://<canonical-hostname>:<port>/v1/turn``. On this
box — and on every stock Debian / Ubuntu / WSL install — the machine's
own hostname does NOT resolve to the loopback address that the a2a
sidecar binds::

    $ getent hosts ywata-note-win
    127.0.1.1   ywata-note-win.localdomain ywata-note-win     <-- .1.1

    a2a/_server.py:  host: str = "127.0.0.1"                  <-- .0.1

    127.0.0.1:19017        -> OPEN
    ywata-note-win:19017   -> CONNECTION REFUSED

``127.0.1.1`` is a loopback address, but it is a DIFFERENT loopback
address from the one the sidecar listens on, so every connection to the
derived URL is refused. Not flaky — deterministic, for every local
consumer, on every box that follows the Debian self-host convention
(``/etc/hosts``: ``127.0.1.1  <fqdn> <hostname>``).

The fix, and why NOT ``0.0.0.0``
================================
Two options existed: (a) derive ``127.0.0.1`` for local callers, or
(b) rebind every agent's a2a sidecar to ``0.0.0.0``. We chose (a).

Binding the sidecar to ``0.0.0.0`` would expose EVERY agent's
``/v1/turn`` — an endpoint that injects a prompt into a live Claude
session — on every interface of the host. sac's whole transport doctrine
is loopback-only, with orochi's tunnel/VPN mesh as the sanctioned
external path (SAC_OROCHI_SCOPES.md §4.4); ``sac listen`` itself refuses
a non-loopback bind without an explicit ``--allow-non-loopback``. Fixing
an address-derivation bug by widening a listener's exposure would trade
a connectivity bug for a security regression. Deriving the address the
sidecar actually binds costs nothing and changes no listener.

So: a host that NAMES THIS MACHINE is normalised to ``127.0.0.1``. A
genuinely REMOTE host keeps its name (a cross-host peer must still be
addressed by hostname, and its URL is consumed over the tunnel mesh).
"""

from __future__ import annotations

import ipaddress
import socket

__all__ = [
    "LOOPBACK_HOST",
    "is_local_host",
    "local_host_aliases",
]

# The address every local a2a sidecar (a2a/_server.py) actually binds,
# and the ONLY loopback address a derived local URL may use. NOT
# ``localhost`` (which can resolve to ::1 first) and emphatically NOT
# the machine's own hostname (→ 127.0.1.1 on Debian/Ubuntu/WSL).
LOOPBACK_HOST = "127.0.0.1"

# Names that mean "this machine" regardless of what the box is called.
# A wildcard bind (``0.0.0.0`` / ``::``) is reachable ON loopback, so it
# is normalised too.
_ALWAYS_LOCAL = frozenset(
    {
        "",
        "localhost",
        "localhost.localdomain",
        "0.0.0.0",
        "::",
    }
)


def local_host_aliases() -> frozenset[str]:
    """Every name (lower-cased) that denotes THIS machine.

    Sources: the always-local literals above, this host's ``gethostname``
    (both the FQDN-ish form and its short label), and sac's own canonical
    host from ``_state.host_config`` — the SAME name every ``state.db``
    write stamps as self-identity, which is exactly the name that ends up
    in an ``instances`` row's ``host`` column and therefore the name
    ``resolve_a2a_host`` hands to ``derive_turn_url``.

    Deliberately does NOT call :func:`socket.getfqdn` — that can trigger
    a blocking reverse-DNS lookup, and nothing on the registry-response
    path may block.
    """
    names: set[str] = set(_ALWAYS_LOCAL)

    try:
        hostname = socket.gethostname()
    except OSError:  # stx-allow: fallback (reason: an unresolvable own-hostname must not break URL derivation — we simply learn no extra alias)
        hostname = ""
    if hostname:
        names.add(hostname)
        names.add(hostname.split(".", 1)[0])

    try:
        from .._state.host_config import load as load_host_config

        canonical = load_host_config().canonical_host()
    except Exception:  # stx-allow: fallback (reason: best-effort — a host_config failure must never block the /agents response; we just lose one alias)
        canonical = ""
    if canonical:
        names.add(canonical)
        names.add(canonical.split(".", 1)[0])

    return frozenset(name.lower() for name in names if name is not None)


def is_local_host(host: str | None, *, aliases: frozenset[str] | None = None) -> bool:
    """Return ``True`` iff ``host`` names THIS machine.

    True for: the always-local literals, this box's own hostname (short or
    long), sac's canonical host — and for ANY loopback IP literal.

    That last clause is the crux of the bug: ``127.0.1.1`` IS a loopback
    address, so a naive ``host == "127.0.0.1"`` check would miss it and
    happily emit an unreachable URL. Every address in ``127.0.0.0/8`` (and
    ``::1``) is only reachable from this machine, so all of them are
    "local" — and all of them must be normalised to the ONE loopback
    address the sidecar is actually bound to.

    ``aliases`` is injectable so the predicate can be exercised without
    depending on the test runner's real hostname.
    """
    if host is None:
        return False
    candidate = host.strip().lower()
    if candidate in (aliases if aliases is not None else local_host_aliases()):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Not an IP literal — it was already checked against the aliases.
        return False
