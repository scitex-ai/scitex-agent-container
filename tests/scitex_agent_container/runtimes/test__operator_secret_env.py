"""Operator secret pass-through into the Hermes profile secret scope.

TDD for the sac-agent-full-permissions slice: names from an explicit
allowlist are resolved from the trusted pool at launch. Fake key names
and values throughout — no real secret ever appears here. No mocks and
no ``monkeypatch`` (STX-NM002 / PA-306 §3): the pool is pointed at a
real temp secrets file via ``SAC_SECRETS_ENVRC`` with save/restore, and
the name list is an explicit function argument.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.runtimes._operator_secret_env import operator_secret_env


@pytest.fixture
def secrets_envrc() -> Iterator[None]:
    """Save/restore ``SAC_SECRETS_ENVRC`` so a test may set it freely."""
    key = "SAC_SECRETS_ENVRC"
    saved = os.environ.get(key)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def _real_pool_file(tmp_path: Path, body: str) -> None:
    """Write a REAL secrets file and point ``SAC_SECRETS_ENVRC`` at it."""
    pool = tmp_path / "pool.src"
    pool.write_text(body, encoding="utf-8")
    os.environ["SAC_SECRETS_ENVRC"] = str(pool)


def test_pass_through_includes_present_pool_keys_by_name(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange: the pool carries one allowlisted name (fake value).
    _real_pool_file(tmp_path, "export SAC_TEST_ALPHA_KEY=aaa\n")
    # Act
    with caplog.at_level(logging.DEBUG):
        out = operator_secret_env(names=("SAC_TEST_ALPHA_KEY", "SAC_TEST_MISSING_KEY"))
    # Assert: exactly the present key, nothing invented.
    assert out == {"SAC_TEST_ALPHA_KEY": "aaa"}


def test_missing_names_warn_by_name_without_values(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    _real_pool_file(tmp_path, "export SAC_TEST_ALPHA_KEY=aaa\n")
    # Act
    with caplog.at_level(logging.DEBUG):
        operator_secret_env(names=("SAC_TEST_ALPHA_KEY", "SAC_TEST_MISSING_KEY"))
    # Assert: the missing NAME is reported; its (unknown) value cannot leak.
    assert any("SAC_TEST_MISSING_KEY" in r.getMessage() for r in caplog.records)


def test_values_never_reach_the_log(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    _real_pool_file(tmp_path, "export SAC_TEST_ALPHA_KEY=super-secret-aaa\n")
    # Act
    with caplog.at_level(logging.DEBUG):
        operator_secret_env(names=("SAC_TEST_ALPHA_KEY",))
    # Assert
    assert not any("super-secret-aaa" in r.getMessage() for r in caplog.records)
