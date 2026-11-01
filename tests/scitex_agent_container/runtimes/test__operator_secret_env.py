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

from scitex_agent_container.runtimes._operator_secret_env import (
    operator_secret_env,
    scope_allows_full,
)


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


def test_full_scope_passes_every_file_defined_name(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the pool file defines a non-allowlisted key; ambient has junk.
    _real_pool_file(tmp_path, "export SAC_TEST_EXTRA_KEY=eee\n")
    os.environ["SAC_TEST_AMBIENT_JUNK"] = "junk"
    try:
        # Act
        out = operator_secret_env(full=True)
        # Assert — file-defined names pass (values resolve), ambient junk does not.
        assert (
            out.get("SAC_TEST_EXTRA_KEY") == "eee"
            and "SAC_TEST_AMBIENT_JUNK" not in out
        )
    finally:
        os.environ.pop("SAC_TEST_AMBIENT_JUNK", None)


def test_full_scope_still_excludes_ambient_only_names(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — an empty (trusted) pool plus ambient-only junk.
    _real_pool_file(tmp_path, "")
    os.environ["SAC_TEST_AMBIENT_ONLY"] = "junk"
    try:
        # Act
        out = operator_secret_env(full=True)
        # Assert — full means file-defined, never environment-inherited.
        assert "SAC_TEST_AMBIENT_ONLY" not in out
    finally:
        os.environ.pop("SAC_TEST_AMBIENT_ONLY", None)


def test_scope_mapping_accepts_full_and_capsule():
    # Arrange
    full = scope_allows_full("full")
    # Act
    capsule = scope_allows_full("capsule")
    # Assert — one fact: the closed set maps correctly.
    assert full is True and capsule is False


def test_scope_mapping_rejects_unknown_values():
    # Arrange — a typo that must never silently starve or over-share.
    # Act
    # Assert
    with pytest.raises(ValueError):
        scope_allows_full("everything")


def test_default_optional_slots_are_not_missing_credential_warnings(
    tmp_path, secrets_envrc, caplog
):
    # Arrange: a trusted pool need not configure every supported provider.
    from scitex_agent_container.runtimes._operator_secret_env import (
        OPERATOR_SECRET_ENV_NAMES,
    )

    _real_pool_file(
        tmp_path,
        "".join(f"export {name}=\n" for name in OPERATOR_SECRET_ENV_NAMES)
        + "export SAC_TEST_EXTRA_KEY=present\n",
    )
    # Act
    with caplog.at_level(logging.DEBUG):
        operator_secret_env(full=True)
    # Assert: absent optional names remain inspectable without warning fatigue.
    assert any(
        r.levelno == logging.DEBUG and "missing from the pool" in r.getMessage()
        for r in caplog.records
    ) and not any(r.levelno >= logging.WARNING for r in caplog.records)
