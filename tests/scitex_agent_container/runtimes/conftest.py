"""Runtimes-tests isolation: default ``SAC_HOST_SKILLS_DIR`` to a
non-existent path so that :func:`runtimes._to_home.deploy_to_home` and
:func:`runtimes._to_home.materialize_to_home` do not auto-discover the
developer's real ``~/.claude/skills`` and materialize it into every
test's ``tmp_path``.

Tests that want to exercise the host-skills resolution (see
``test__skills_resolve.py``) override this default by calling
``env_save_restore.set(_ENV, <real path>)`` after the autouse fixture
fires — that subsequent set wins for the rest of the test and is
restored at teardown by ``env_save_restore``.

This isolation is necessary because :mod:`runtimes._skills_resolve`
intentionally falls back to ``~/.claude/skills`` when no env override
is set; without this fixture the developer's host skills would leak
into every existing ``_to_home`` test (huge tree, slow, and on a small
tmpfs it can ``ENOSPC``).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_host_skills_dir(env_save_restore):
    """Force ``SAC_HOST_SKILLS_DIR`` to a non-existent path for every
    test under ``tests/scitex_agent_container/runtimes/``.

    Order: this fixture depends on ``env_save_restore`` so the set is
    tracked and reverted at teardown. Individual tests can override the
    value (``env_save_restore.set(...)``) and ``env_save_restore`` will
    still restore the original env at teardown — the bookkeeping uses
    the first observed value, not the last.
    """
    env_save_restore.set(
        "SAC_HOST_SKILLS_DIR", "/nonexistent-host-skills-test-isolation"
    )
    yield
