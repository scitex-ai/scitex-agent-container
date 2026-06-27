"""Tests for ``runtimes._apptainer_listen_env.listen_env_flags``.

``listen_env_flags`` builds the ``--env`` flags ``build_run_argv`` appends
to the ``apptainer exec`` argv. It UNCONDITIONALLY injects the bus-listen
base URL plus — as of the persistent-testmon-cache change — the
``SCITEX_TESTMON_CACHE_ROOT`` env var pointing at the container-side bind
destination ``/home/agent/.cache/scitex-testmon``. scitex-dev's pre-commit
hook reads that var so testmon's cache survives the fresh-git-worktree
churn the develop-pin hook forces.

No mocks — a tiny in-test config stand-in (``types.SimpleNamespace``) plus
a sandboxed ``$HOME`` so the bearer-token lookup resolves to an absent
file (the no-bus / warn-only branch), which keeps the test free of any
host bus token. STX-TQ002 AAA + STX-TQ007 one-assert.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from scitex_agent_container.runtimes._apptainer_listen_env import (
    listen_env_flags,
)


@pytest.fixture
def no_bus_config() -> SimpleNamespace:
    """A config whose ``claude.channels`` is empty (no ``server:sac``).

    With no bus channel requested, an absent bearer token is harmless —
    ``listen_env_flags`` warns and returns the base-URL + testmon-cache
    flags instead of raising, so the test never needs a real token file.
    """
    return SimpleNamespace(claude=SimpleNamespace(channels=[]))


@pytest.fixture
def sandboxed_home(tmp_path: Path) -> Iterator[Path]:
    """Yield a tmp_path-rooted ``$HOME`` so the bearer-token path is absent."""
    prev = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if prev is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = prev


def test_listen_env_flags_injects_testmon_cache_root_var(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — fixtures supply an empty-channel config + sandboxed $HOME.
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — the container-side testmon cache path is injected as an env var.
    assert (
        "SCITEX_TESTMON_CACHE_ROOT=/home/agent/.cache/scitex-testmon" in flags
    )


def test_listen_env_flags_testmon_var_follows_an_env_token(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — apptainer consumes ``--env KEY=VALUE`` as two argv tokens;
    # the testmon var must be preceded by a literal ``--env`` flag.
    flags = listen_env_flags(no_bus_config)
    # Act
    idx = flags.index(
        "SCITEX_TESTMON_CACHE_ROOT=/home/agent/.cache/scitex-testmon"
    )
    # Assert
    assert flags[idx - 1] == "--env"


def test_listen_env_flags_still_injects_base_url(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — fixtures supply an empty-channel config + sandboxed $HOME.
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — the pre-existing base-URL injection is unregressed.
    assert any(f.startswith("SAC_LISTEN_BASE_URL=") for f in flags)


def test_listen_env_flags_still_injects_testmon_cache_root(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — fixtures supply an empty-channel config + sandboxed $HOME.
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — the testmon-cache injection is unregressed.
    assert (
        "SCITEX_TESTMON_CACHE_ROOT=/home/agent/.cache/scitex-testmon" in flags
    )


def test_listen_env_flags_injects_genai_base_url(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — fixtures supply an empty-channel config + sandboxed $HOME.
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — the host-tunneled qwen fallback base URL is injected.
    assert "SCITEX_GENAI_BASE_URL=http://127.0.0.1:4000/v1" in flags


def test_listen_env_flags_genai_base_url_follows_an_env_token(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — apptainer consumes ``--env KEY=VALUE`` as two argv tokens;
    # the genai base URL must be preceded by a literal ``--env`` flag.
    flags = listen_env_flags(no_bus_config)
    # Act
    idx = flags.index("SCITEX_GENAI_BASE_URL=http://127.0.0.1:4000/v1")
    # Assert
    assert flags[idx - 1] == "--env"


def test_listen_env_flags_injects_no_genai_api_key(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — only the base URL half is injected here; the qwen API key
    # is gated on a separate operator security decision and must NOT leak.
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — no genai/qwen API-key env var is present.
    assert not any(
        "GENAI_API_KEY" in f or "QWEN_API_KEY" in f for f in flags
    )
