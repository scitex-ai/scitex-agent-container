"""Recognise the live fleet database in a DSN, so no test can ever reach it.

Split out of :mod:`tests._store_isolation`, which uses it at import (to refuse
a declared test DSN that names the fleet) and after every test (to fail a test
that left the process pointed at the fleet).

MEASURED 2026-11-19. A kernel trace on scitex-compute-02 showed this suite,
running on the org self-hosted runners, opening ~1,330 connections in ten
minutes to the live pooler ``scitex-primary:55432``. The runners use the real
``$HOME``, so the same processes that drew pgbouncer's ``bouncer config error``
(no PGUSER -> the OS user, which is no role) could equally authenticate with any
role in the runner user's ``~/.pgpass``. A test suite one variable away from
writing production state.

Pure parsing, no psycopg: this runs at plugin import, before anything may
connect, and psycopg is deliberately not a hard dependency of the suite.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

#: Matched by HOST or by PORT, and both on purpose. ``scitex-primary`` and
#: ``cards-primary`` are the fleet's names for the primary (100.64.0.5 on the
#: tailnet); 55432 is the pooler and 55433 the server behind it. A loopback DSN
#: on 55432 is the pooler too on scitex-nas-03, and was a fleet replica on every
#: compute host until 2026-10-06 — so the port alone is disqualifying, whatever
#: host the DSN spells.
FLEET_DB_HOSTS = frozenset({"scitex-primary", "cards-primary", "100.64.0.5"})
FLEET_DB_PORTS = frozenset({55432, 55433})


def _uri_endpoints(dsn: str) -> tuple[list[str], list[str]]:
    """Hosts and ports of a ``postgresql://`` URI, including ``?host=``."""
    parts = urlsplit(dsn)
    hosts: list[str] = []
    ports: list[str] = []
    for item in filter(None, parts.netloc.rpartition("@")[2].split(",")):
        if item.startswith("["):
            host, _, rest = item[1:].partition("]")
            port = rest.lstrip(":")
        else:
            host, _, port = item.partition(":")
        hosts.append(unquote(host))
        ports.append(port)
    query = parse_qs(parts.query)
    for value in query.get("host", []) + query.get("hostaddr", []):
        hosts += value.split(",")
    for value in query.get("port", []):
        ports += value.split(",")
    return hosts, ports


def _keyword_endpoints(dsn: str) -> tuple[list[str], list[str]]:
    """Hosts and ports of a libpq ``key=value`` connection string."""
    hosts: list[str] = []
    ports: list[str] = []
    for key, raw in re.findall(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn):
        value = raw.strip("'")
        if key in ("host", "hostaddr"):
            hosts += value.split(",")
        elif key == "port":
            ports += value.split(",")
    return hosts, ports


def dsn_endpoints(dsn: str) -> list[tuple[str, int | None]]:
    """``(host, port)`` pairs a libpq DSN names, URI or key=value form.

    A port of ``None`` means "not stated anywhere"; an unstated host or port
    falls back to ``PGHOST``/``PGPORT``, exactly as libpq resolves it.
    """
    hosts, ports = _uri_endpoints(dsn) if "://" in dsn else _keyword_endpoints(dsn)
    hosts = [h for h in hosts if h] or [os.environ.get("PGHOST", "")]
    ports = [p for p in ports if p] or [os.environ.get("PGPORT", "")]
    out: list[tuple[str, int | None]] = []
    for i, host in enumerate(hosts):
        port = ports[min(i, len(ports) - 1)].strip()
        out.append((host.strip().lower(), int(port) if port.isdigit() else None))
    return out


def fleet_endpoint_named_by(dsn: str | None) -> str | None:
    """Describe the fleet endpoint ``dsn`` points at, or ``None`` if it is safe."""
    if not dsn:
        return None
    for host, port in dsn_endpoints(dsn):
        if host in FLEET_DB_HOSTS:
            return f"host {host!r} is the fleet database"
        if port in FLEET_DB_PORTS:
            return f"port {port} is the fleet database (pooler 55432 / server 55433)"
    return None


class FleetDatabaseRefused(pytest.UsageError):
    """A test database target named the live fleet database. Never a skip."""


def refuse_fleet_dsn(dsn: str | None, *, source: str) -> None:
    """Raise :class:`FleetDatabaseRefused` when ``dsn`` names the fleet DB."""
    why = fleet_endpoint_named_by(dsn)
    if why:
        raise FleetDatabaseRefused(
            f"{source} points at the LIVE fleet database ({why}): {dsn!r}. "
            "Tests must use a throwaway PostgreSQL — set SAC_TEST_PG_DSN to one "
            "(CI gets a postgres service container), or leave it unset and the "
            "PostgreSQL tests skip."
        )


def fleet_env_leaks(environ: "os._Environ[str] | dict[str, str]") -> list[str]:
    """Environment variables that would send libpq or the store to the fleet."""
    checks = (
        ("SCITEX_STORE_DSN", environ.get("SCITEX_STORE_DSN")),
        (
            "PGHOST",
            f"host={environ['PGHOST']} port=5432" if environ.get("PGHOST") else None,
        ),
        (
            "PGPORT",
            f"host=localhost port={environ['PGPORT']}"
            if environ.get("PGPORT")
            else None,
        ),
    )
    return [
        f"{name}={environ[name]!r} ({why})"
        for name, probe in checks
        if (why := fleet_endpoint_named_by(probe))
    ]
