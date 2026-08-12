"""The secrets pool read, and whether a MISS in it means anything.

Covers ``runtimes/_secret_pool.read_pool``. A HIT is self-validating; the
question with three answers is what a MISS means, and ``PoolRead.trusted`` is
that answer.

WHY THIS FLAG EXISTS AT ALL (card
``sac-cct-token-slot-mismatch-and-env-fold-20260812``): after a relocation,
three consecutive diagnoses said "there is no token on compute-04". The pool
file was on that host, complete. What was missing was ``SAC_SECRETS_ENVRC`` in
the LAUNCHING process, so sac read the bare process env and could not tell the
difference. The operator's correction — 「04 にトークンが無い」と私は言ったが誤り。
**起動プロセスに無かった**が正しい。この区別がバグそのもの。 — is the whole
specification for these tests.

Real temp pool files and real bash sourcing, exactly as
``test__cct_token_pool.py`` does — no mocks (PA-306). STX-TQ002 AAA markers,
STX-TQ007 one assert per test. Slot names use a ``ZZ_``-prefixed namespace so
an operator shell's real pool vars can never collide with the fixtures.

Named ``test__secret_pool.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_secret_pool.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._secret_pool import (
    _SECRETS_ENVRC_VAR,
    _pool_env,
    read_pool,
)


@pytest.fixture
def secrets_envrc() -> Iterator[None]:
    """Save/restore ``SAC_SECRETS_ENVRC`` so a test may set it freely."""
    saved = os.environ.get(_SECRETS_ENVRC_VAR)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_SECRETS_ENVRC_VAR, None)
        else:
            os.environ[_SECRETS_ENVRC_VAR] = saved


def _real_pool_file(tmp_path: Path, body: str) -> None:
    """Write a REAL secrets file and point ``SAC_SECRETS_ENVRC`` at it."""
    pool = tmp_path / "pool.src"
    pool.write_text(body, encoding="utf-8")
    os.environ[_SECRETS_ENVRC_VAR] = str(pool)


# ---------------------------------------------------------------------------
# a conclusive read
# ---------------------------------------------------------------------------


def test_a_sourced_secret_file_is_a_conclusive_read(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a real pool file that a real bash can source.
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_TRUST=zz-value\n")
    # Act
    read = read_pool()
    # Assert — sac read the pool it meant to read, so a miss would mean something.
    assert read.trusted is True


def test_a_sourced_secret_file_yields_its_slots(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_PRESENT=zz-value\n")
    # Act
    read = read_pool()
    # Assert
    assert read.env.get("CCT_BOT_TOKEN_ZZ_PRESENT") == "zz-value"


def test_a_conclusive_read_carries_no_complaint(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_QUIET=zz-value\n")
    # Act
    read = read_pool()
    # Assert — ``detail`` is the reason a read is UNtrusted; a good read has none.
    assert read.detail == ""


# ---------------------------------------------------------------------------
# an INCONCLUSIVE read — the relocation shape
# ---------------------------------------------------------------------------


def test_no_resolvable_secret_file_is_an_inconclusive_read(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the var is set (so the $HOME default is not consulted) but
    # points nowhere, which is how a unit / ssh / cron caller sees the pool.
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    # Act
    read = read_pool()
    # Assert — sac never opened a pool file, so it learned nothing about absence.
    assert read.trusted is False


def test_an_inconclusive_read_still_carries_the_process_env(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the process env can still PROVE a slot present; it just cannot
    # prove one absent, which is why the flag is separate from the mapping.
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    os.environ["ZZ_POOL_PROBE"] = "zz-here"
    try:
        # Act
        read = read_pool()
    finally:
        os.environ.pop("ZZ_POOL_PROBE", None)
    # Assert
    assert read.env.get("ZZ_POOL_PROBE") == "zz-here"


def test_an_inconclusive_read_blames_the_launching_process(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the detail must point at the vantage point, not at the host,
    # because "there is no token on this host" was the wrong diagnosis three
    # times running.
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    # Act
    read = read_pool()
    # Assert
    assert "LAUNCHING PROCESS" in read.detail


def test_an_inconclusive_read_refuses_to_claim_absence(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    os.environ[_SECRETS_ENVRC_VAR] = str(tmp_path / "absent.src")
    # Act
    read = read_pool()
    # Assert
    assert "never proves one ABSENT" in read.detail


# ---------------------------------------------------------------------------
# back-compat
# ---------------------------------------------------------------------------


def test_pool_env_still_returns_the_bare_mapping(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — callers that only ask "is this slot here?" keep working.
    _real_pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_COMPAT=zz-value\n")
    # Act
    env = _pool_env()
    # Assert
    assert env.get("CCT_BOT_TOKEN_ZZ_COMPAT") == "zz-value"
