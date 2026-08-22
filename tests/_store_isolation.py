"""PostgreSQL store isolation, loaded as a PLUGIN so no directory escapes it.

WHY A PLUGIN AND NOT A CONFTEST. This lived in
``tests/scitex_agent_container/conftest.py`` until 2026-08-20, and that
placement is what let the incident described below happen a second time.
``tests/smoke/``, ``tests/develop/``, ``tests/integration/`` and
``tests/examples/`` are SIBLINGS of that directory, not children, so the
autouse guard never reached them. When ``acl_deny_notify`` moved to
PostgreSQL, the smoke suite began writing fixture rows — ``alpha``,
``beta``, ``gamma`` — into the live per-host store, exactly as the lifecycle
tests had under #1154.

A whole-suite run was protected only by accident: collecting the
``scitex_agent_container`` package imports its conftest, and the
module-level assignment below is process-wide. Running ``tests/smoke/``
ALONE was not protected at all. Protection by import order is not
protection.

Registered from ``pyproject.toml`` (``addopts = "... -p
tests._store_isolation"``) rather than from ``tests/conftest.py``, because
that conftest is already over the per-file line cap and this file has no
business making it worse. ``-p`` also loads earlier and unconditionally —
it does not depend on which directory the run happens to collect.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest


# ----------------------------------------------------------------------
# PostgreSQL isolation for the sqlite -> Postgres migration (2026-08-19)
# ----------------------------------------------------------------------

#: The per-host store. Loopback only — every fleet PostgreSQL refuses
#: non-local connections at pg_hba, measured 2026-08-19.
PG_BASE_DSN = os.environ.get(
    "SAC_TEST_PG_DSN", "postgresql://scitex_cards@127.0.0.1:55432/scitex"
)

#: The password file, resolved AT IMPORT and pinned for the fixture below.
#:
#: MEASURED 2026-08-20. `tests/.../_listen/test__acl_approve_flow.py` sandboxes
#: `HOME` to a tmp_path — correctly, it is testing host-file behaviour. libpq
#: finds its password file via `~/.pgpass`, so under that sandbox the lookup
#: lands in an empty directory and the fixture's own CREATE SCHEMA fails with
#: `fe_sendauth: no password supplied`. The same fixture works everywhere else,
#: which is what made it look like a fixture bug rather than an interaction.
#:
#: Resolving here — while HOME is still the real one, before any test can move
#: it — and passing it explicitly makes the fixture independent of a variable
#: tests are entitled to change. `PGPASSFILE` is libpq's own override and takes
#: precedence over the HOME lookup.
_PGPASS_AT_IMPORT = os.environ.get("PGPASSFILE") or os.path.expanduser("~/.pgpass")

#: A DSN that cannot reach anything, on purpose. Port 1 refuses instantly, and
#: the database name is written to be legible in the error a stray write
#: produces — the message itself states the rule it just enforced.
_UNREACHABLE_DSN = (
    "postgresql://sac_tests@127.0.0.1:1/tests_must_not_write_to_the_fleet_store"
)

# SET AT IMPORT, NOT ONLY IN A FIXTURE, and the difference is measured.
#
# The first version of this guard was function-scoped only. Re-running the full
# package suite with it dropped the pollution from 46 rows to 4 — better, and
# still not zero. Every targeted re-run (six lifecycle modules, four whole
# directories, five store-touching modules) then wrote ZERO rows, so the
# remaining four were not any single module: they came from writes that happen
# OUTSIDE a function-scoped fixture's window — collection, and module- or
# session-scoped fixtures, which are set up before the first function fixture
# runs and torn down after the last one ends.
#
# A conftest is imported before anything under its directory is collected, so
# assigning here covers the whole pytest process, including those windows. The
# fixture below stays: it re-asserts the value per test, so a test that changes
# the variable cannot leak the change into its neighbours.
os.environ["SCITEX_STORE_DSN"] = _UNREACHABLE_DSN


@pytest.fixture(autouse=True)
def _no_accidental_fleet_store_writes() -> Iterator[None]:
    """Point every test at a store that cannot exist, unless it asks otherwise.

    MEASURED INCIDENT, 2026-08-20. When the birth certificate moved to
    PostgreSQL (#1154), ``write_birth_certificate`` lost its ``db_path``. Tests
    that had been threading a ``tmp_path`` SQLite file were suddenly resolving
    the FLEET DSN instead, and one full-suite run on scitex-compute-04 wrote
    **46 rows into the live incarnations store** — ``alpha``, ``zombie``,
    ``born-1``..``born-4``, ``pid-*``, ``rec-*``, ``screen-*``, every one a
    fixture name. They were removed with the store's own ``hide`` verb.

    The rows were the small half of the problem. sac's CI runs on SELF-HOSTED
    runners that sit on the fleet hosts, so every future CI run would have
    written its fixtures into whichever machine it landed on — a test suite
    quietly editing production state, on a schedule.

    WHY AN UNREACHABLE DSN RATHER THAN A THROWAWAY SCHEMA. A schema per test
    would CONNECT, which makes every test in this package depend on PostgreSQL
    being up — hundreds of tests that never touch a store would start failing
    on a database outage. This costs nothing and cannot be skipped: there is no
    server on port 1, so a stray write raises immediately.

    WHY THE LIVE DSN IS NOT SIMPLY LEFT IN PLACE FOR "HARMLESS" WRITES. It was,
    and that IS the incident. A best-effort caller swallows the failure, so a
    test that writes to the fleet store looks identical to one that does not —
    right up until someone counts the rows.

    Tests that need a REAL store take ``pg_schema``, which depends on this
    fixture and overwrites the variable afterwards, so the ordering is
    guaranteed by the dependency rather than by pytest's autouse rules.
    """
    key = "SCITEX_STORE_DSN"
    saved = os.environ.get(key)
    os.environ[key] = _UNREACHABLE_DSN
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


@pytest.fixture()
def pg_schema(_no_accidental_fleet_store_writes: None) -> Iterator[str]:
    """A throwaway PostgreSQL schema, wired in via ``SCITEX_STORE_DSN``.

    Yields the schema name. Anything a module-under-test writes through
    ``scitex_dev.store`` lands here and is dropped afterwards, so the live
    fleet state is never touched.

    NOT AUTOUSE, and the two fixtures above are: this one is opt-in because
    it CONNECTS, and a test that does not need PostgreSQL must not fail
    because PostgreSQL is down.

    A SCHEMA rather than a database, deliberately: creating a database needs
    ``CREATEDB`` and the fleet is not uniform there (compute-03's
    ``scitex_cards`` role has ``rolcreatedb=False``), so a create-a-database
    fixture would pass on three runners and fail on the fourth — a flake
    that looks like the code. The name carries a uuid because the three-way
    python matrix can put concurrent jobs on ONE runner.

    ``psycopg`` is imported INSIDE the fixture, not at module scope. A
    top-level import here would make it a hard dependency of every test in
    this package, so a missing psycopg would turn into a collection error
    for hundreds of tests that never touch a database.

    Real ``os.environ`` save/restore, not ``monkeypatch`` — PA-306 forbids
    mocks, and the point is that the REAL resolver reads the REAL variable.

    (``_state/test_state_db_verdict_dedup.py`` still carries its own copy of
    this fixture, written before this shared one existed. It is the same
    code; consolidating it is a tidy-up for a PR that already touches that
    file, not for this one.)
    """
    import uuid

    import psycopg

    # Pin PGPASSFILE for the whole fixture: a test that sandboxes HOME (several
    # legitimately do) would otherwise strand libpq's ~/.pgpass lookup.
    pgpass_key = "PGPASSFILE"
    saved_pgpass = os.environ.get(pgpass_key)
    os.environ[pgpass_key] = _PGPASS_AT_IMPORT

    schema = "sac_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')

    key = "SCITEX_STORE_DSN"
    saved = os.environ.get(key)
    os.environ[key] = f"{PG_BASE_DSN}?options=-csearch_path%3D{schema}"
    try:
        yield schema
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        with psycopg.connect(PG_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        if saved_pgpass is None:
            os.environ.pop(pgpass_key, None)
        else:
            os.environ[pgpass_key] = saved_pgpass
