"""Package-wide test conftest — neutralise production-env pollution.

The test process itself runs inside the proj-scitex-agent-container
apptainer SIF (the dev / agent runtime ships pre-built). Production-
intended env vars (``APPTAINER_CONTAINER`` for in-SIF detection,
``SCITEX_AGENT_CONTAINER_AGENT`` for the agent's own identity) are
therefore set in the test process's env. Tests that read those vars
expecting "bare host, no agent identity" silently pick up the running
agent's values and fail with hard-to-debug "wrong file path" /
"unexpected branch" errors.

Two autouse fixtures here. Both yield with the polluting vars
cleared and restore on teardown — never delete pre-existing values
permanently. Tests that *want* the polluting var set drop it back
themselves (e.g. ``_lifecycle/test__in_sif_broker.py::sif_env``).

(1) ``_clear_in_sif_env`` — clears ``APPTAINER_CONTAINER`` /
``SINGULARITY_CONTAINER``. Required by the SAC-from-SAC broker
(operator-mandated 2026-06-01): every ``agent_start`` call now
detects in-SIF and routes through the host listen instead of the
local runtime. Without clearing here, the lifecycle tests'
hand-rolled runtime fakes are never reached.

(2) ``_clear_agent_identity`` — clears ``SCITEX_AGENT_CONTAINER_AGENT``
/ ``SAC_AGENT``. The statusline ``_agent_name()`` reads these to
discover which agent the running session belongs to. With the running
agent's name leaking through, tests that set ``CLAUDE_AGENT_ID`` to
control the persist filename end up writing to the running agent's
file instead — assertions on the test-controlled filename then fail
with ``FileNotFoundError``.

Both fixtures cover the WHOLE package's test tree (this conftest is
at ``tests/scitex_agent_container/``). Putting them here is simpler
than re-implementing under every nested test dir.

No production code is mutated by these fixtures; they only touch the
test process env.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

_IN_SIF_KEYS = ("APPTAINER_CONTAINER", "SINGULARITY_CONTAINER")
_AGENT_IDENTITY_KEYS = (
    "SCITEX_AGENT_CONTAINER_AGENT",
    "SAC_AGENT",
)


def _save_restore_yield(keys: tuple[str, ...]) -> Iterator[None]:
    """Real save / clear / yield / restore for a set of env keys."""
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


@pytest.fixture(autouse=True)
def _clear_in_sif_env() -> Iterator[None]:
    """Yield with in-SIF env vars cleared; restore on teardown."""
    yield from _save_restore_yield(_IN_SIF_KEYS)


@pytest.fixture(autouse=True)
def _clear_agent_identity() -> Iterator[None]:
    """Yield with ``SAC_AGENT`` / long-form cleared; restore on teardown.

    Statusline + a couple of identity-keyed CLIs read this; the running
    agent's value would otherwise win over test-controlled overrides.
    """
    yield from _save_restore_yield(_AGENT_IDENTITY_KEYS)


# ----------------------------------------------------------------------
# PostgreSQL isolation for the sqlite -> Postgres migration (2026-08-19)
# ----------------------------------------------------------------------

#: The per-host store. Loopback only — every fleet PostgreSQL refuses
#: non-local connections at pg_hba, measured 2026-08-19.
PG_BASE_DSN = os.environ.get(
    "SAC_TEST_PG_DSN", "postgresql://scitex_cards@127.0.0.1:55432/scitex"
)


@pytest.fixture()
def pg_schema() -> Iterator[str]:
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
