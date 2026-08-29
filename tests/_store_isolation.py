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

import getpass
import os
from typing import Iterator

import pytest

# ----------------------------------------------------------------------
# PostgreSQL isolation for the sqlite -> Postgres migration (2026-08-19)
# ----------------------------------------------------------------------

#: The per-host store. Loopback only — every fleet PostgreSQL refuses
#: non-local connections at pg_hba, measured 2026-08-19.
#:
#: NO USERINFO IN THE DSN (2026-08-24). This used to read
#: ``postgresql://scitex_cards@...``. That role was the fleet's single shared
#: superuser and it has been demoted to NOLOGIN, so a DSN naming it now fails
#: to authenticate everywhere at once. Identity travels in ``PGUSER`` instead
#: (the same convention every agent launches with — see
#: ``runtimes/_pg_identity_env``), and the password stays in ``~/.pgpass``
#: where libpq finds it without any credential entering this file.
PG_BASE_DSN = os.environ.get("SAC_TEST_PG_DSN", "postgresql://127.0.0.1:55432/scitex")

#: Was the target DECLARED, or did we fall back to the default?
#:
#: The distinction decides skip-vs-fail, and it is the whole point. "This
#: laptop has no cluster" is a fact about a machine and may be skipped. "The
#: target somebody explicitly configured does not work" is a
#: MISCONFIGURATION, and a misconfiguration that skips is indistinguishable
#: from a pass — which is exactly how this suite came to report green while
#: executing zero PostgreSQL coverage.
PG_DSN_WAS_DECLARED = "SAC_TEST_PG_DSN" in os.environ

#: Set to "1" to make an unusable target a hard FAILURE rather than a skip.
#:
#: Intended for the RELEASE gate. A pull request may proceed with PostgreSQL
#: coverage skipped, because the alternative is a blocked queue; a TAG must
#: not publish having silently run none of it.
PG_REQUIRED = os.environ.get("SAC_TEST_PG_REQUIRED") == "1"

#: The fleet's replication cluster, which tests must NEVER write to.
#:
#: Every node of it — primary and replicas alike — reports this identifier,
#: so one comparison recognises the production cluster no matter which host
#: the DSN happens to resolve to, and no matter which node is primary today.
#: That last part matters: the operator has stated failover is expected
#: (2026-08-26), so a guard naming a HOST would protect the wrong machine the
#: moment the primary moves. Identity travels with the cluster; addresses do
#: not.
#:
#: This exists because the honest fix for CI is to give tests their own
#: throwaway database, and the failure mode of that work is pointing at the
#: real cluster by accident. The cluster lost a primary-key index to a libc
#: mismatch on 2026-08-25; it does not need CI creating and dropping schemas
#: in it as well.
FLEET_SYSTEM_ID = os.environ.get("SAC_TEST_PG_FORBIDDEN_SYSTEM_ID", "7672112238472680366")


def pg_endpoint_port() -> str:
    """The PORT of the database under test, as a locator would print it.

    Four tests assert that ``init_*_schema()`` returns a locator NAMING the
    endpoint it wrote to — the property being that state cannot land somewhere
    without saying where. They each hardcoded ``"55432"``, which was not the
    property but an assumption about the environment: the fleet's loopback
    port. It held only because every runner happened to have the fleet cluster
    there.

    MEASURED 2026-08-26: the moment CI provisioned its own throwaway database
    on an ephemeral port, all four failed with
    ``assert '55432' in 'postgres[host=127.0.0.1 db=postgres port=46313]'``.
    The locator was correct; the expectation was pinned to a machine.

    Deriving it from ``PG_BASE_DSN`` keeps the assertion testing the thing it
    was written to test — the locator names the endpoint — while surviving any
    database the suite is pointed at.
    """
    from urllib.parse import urlsplit

    port = urlsplit(PG_BASE_DSN).port
    return str(port) if port else "5432"


def _default_test_pg_user() -> str:
    """The login role tests authenticate as when nothing declares one.

    Derived, never hardcoded: the roles are ``<os-user>__<consumer>`` and the
    owner varies by machine, so a literal would work on one host and fail on
    the next. ``__cli`` is the interactive/tooling leaf — the same one a shell
    on the host uses.

    It must be a LOGIN role: libpq's own fallback is the bare OS user, which
    in this role tree is the NOLOGIN permission umbrella, so leaving PGUSER
    unset authenticates as a role that deliberately cannot log in.
    """
    return f"{getpass.getuser()}__cli"


#: Resolved at import for the same reason as the password file below: a test
#: that sandboxes the environment must not change who the fixture connects as.
PG_TEST_USER = os.environ.get("PGUSER") or _default_test_pg_user()

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

    # Identity for the fixture's OWN connections. Set alongside PGPASSFILE and
    # for the same reason: the DSN carries no userinfo any more, so without
    # this libpq falls back to the OS user (a NOLOGIN role).
    pguser_key = "PGUSER"
    saved_pguser = os.environ.get(pguser_key)
    os.environ[pguser_key] = PG_TEST_USER

    schema = "sac_test_" + uuid.uuid4().hex[:12]

    def _restore_identity() -> None:
        """Put PGPASSFILE/PGUSER back exactly as they were.

        Hoisted out of the handlers because it now has FOUR call sites and
        one of them is a teardown that previously skipped it — see the
        ``finally`` below.
        """
        for key_, saved_ in ((pgpass_key, saved_pgpass), (pguser_key, saved_pguser)):
            if saved_ is None:
                os.environ.pop(key_, None)
            else:
                os.environ[key_] = saved_

    def _unusable(reason: str, *, hard: bool) -> None:
        """Report an unusable target — loudly, and never silently.

        ``hard`` fails the run; otherwise it skips. Either way the reason
        names the DSN and the role, so a skip on a host that is SUPPOSED to
        have a writable database reads as the misconfiguration it is instead
        of disappearing into a skip count.

        VISIBILITY COMES FROM ``-rs``, NOT FROM AN ANNOTATION, and that is a
        correction rather than a preference. The first version of this
        printed a GitHub ``::warning::`` here. MEASURED on run
        32919218635: the reason text appears **308 times** in the CI log
        while the annotation appears **zero** times — pytest captures fixture
        stdout, so the annotation never reached the step output at all. It
        was decoration that looked like a safeguard. The mechanism that
        actually works is ``-rs`` on the pytest invocation, which prints
        every skip reason in the summary; that is what put those 308 lines
        there.
        """
        _restore_identity()
        message = (
            f"PostgreSQL fixture cannot use {PG_BASE_DSN} as {PG_TEST_USER}: {reason}"
        )
        if hard:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)

    try:
        with psycopg.connect(PG_BASE_DSN, connect_timeout=10, autocommit=True) as conn:
            # Refuse the production cluster BEFORE issuing any DDL. On a
            # replica the CREATE would fail anyway; on the PRIMARY it would
            # succeed, which is the outcome worth preventing.
            row = conn.execute(
                "SELECT system_identifier::text, pg_is_in_recovery() "
                "FROM pg_control_system()"
            ).fetchone()
            if row and row[0] == FLEET_SYSTEM_ID:
                # Same cluster, two very different situations, and collapsing
                # them turns this guard into an outage. Measured 2026-08-26:
                # treating both as fatal made every runner RED, because
                # loopback on a runner IS a fleet replica and always will be.
                if not row[1]:
                    # NOT in recovery -> this is the PRIMARY, where a CREATE
                    # SCHEMA would SUCCEED. That is the only case worth
                    # failing over: the write lands in production.
                    _unusable(
                        f"that is the fleet PRIMARY (system_identifier={row[0]}). "
                        f"Tests must never create schemas in production; point "
                        f"SAC_TEST_PG_DSN at a throwaway database.",
                        hard=True,
                    )
                # In recovery -> a read-only replica. Nothing can be written
                # here, so this is simply "no writable database on this host",
                # which per the operator's 2026-08-26 ruling (one primary, all
                # else read-only replicas) is the PERMANENT shape of every
                # runner until CI provisions its own database.
                _unusable(
                    f"loopback is a read-only replica of the fleet cluster "
                    f"(system_identifier={row[0]}); there is no writable "
                    f"database on this host",
                    hard=PG_REQUIRED or PG_DSN_WAS_DECLARED,
                )
            conn.execute(f'CREATE SCHEMA "{schema}"')
    except psycopg.errors.ReadOnlySqlTransaction as exc:
        # CONNECTED, but the server will not accept writes — a read-only
        # replica. This is NOT the "no cluster here" case below and must not
        # be folded into it: psycopg models it as InternalError, not
        # OperationalError, so the handler that follows never saw it and the
        # tests errored instead. Per the operator's 2026-08-26 ruling the
        # fleet is one primary plus read-only replicas, so loopback on a
        # runner is a replica BY DESIGN and this is now the expected shape
        # until CI provisions its own database.
        _unusable(f"the server is a read-only replica ({exc})", hard=PG_REQUIRED or PG_DSN_WAS_DECLARED)
    except psycopg.OperationalError as exc:
        # SKIP, not fail: not every machine that runs this suite still has a
        # local cluster. The fleet's stores were consolidated onto the primary
        # and one laptop on 2026-08-24, while the self-hosted CI runners live
        # on hosts that no longer keep one — those runners were reporting four
        # ERRORs per job for an absent server rather than for anything in the
        # code under test.
        #
        # This is the same rule the fixture already states for the suite at
        # large ("a test that does not need PostgreSQL must not fail because
        # PostgreSQL is down"), applied one level down: a test that DOES need
        # PostgreSQL still must not fail on a host that legitimately has none.
        # The reason string names the DSN so a skip on a host that is SUPPOSED
        # to have a cluster reads as the misconfiguration it is, instead of
        # disappearing into a skip count.
        #
        # STILL A SKIP BY DEFAULT, but no longer a silent one, and no longer
        # unconditional: if somebody DECLARED SAC_TEST_PG_DSN, an unreachable
        # target is their configuration being wrong, not this machine lacking
        # a cluster, and it fails.
        _unusable(f"no reachable PostgreSQL ({exc})", hard=PG_REQUIRED or PG_DSN_WAS_DECLARED)

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
        # The DROP is wrapped because the identity restore below MUST happen
        # even when it raises. Previously it did not: the connect/DROP sat
        # outside any try with the restore after it, so a server that went
        # read-only or unreachable mid-run left this fixture's PGPASSFILE and
        # PGUSER installed in os.environ for every later test on the same
        # xdist worker. A leaked identity does not fail here; it fails
        # somewhere else, later, as something that looks unrelated.
        try:
            with psycopg.connect(
                PG_BASE_DSN, connect_timeout=10, autocommit=True
            ) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            _restore_identity()
