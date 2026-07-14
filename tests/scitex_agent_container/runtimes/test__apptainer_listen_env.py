"""Tests for ``runtimes._apptainer_listen_env.listen_env_flags``.

``listen_env_flags`` builds the ``--env`` flags ``build_run_argv`` appends
to the ``apptainer exec`` argv. It UNCONDITIONALLY injects the bus-listen
base URL.

It used to also inject ``SCITEX_TESTMON_CACHE_ROOT``, pointing at a host
directory bound rw into every agent container, to accelerate a scitex-dev
pre-commit hook. Both are GONE: scitex-dev's own pre-commit policy calls that
hook broken and referenced by zero repos, its audit rule PS-HOOK-001 forbids
the hook's shape, and sac never used testmon at all. Pre-commit runs fast,
bounded, deterministic checks — it does NOT run the test suite — so there was
never a suite in the commit path for a testmon cache to speed up. The
assertion below is INVERTED to keep the plumbing from quietly returning.

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
from scitex_agent_container.runtimes._mcp_reliability import (
    MCP_STARTUP_TIMEOUT_MS,
)


@pytest.fixture
def no_bus_config() -> SimpleNamespace:
    """A config whose ``claude.channels`` is empty (no ``server:sac``).

    With no bus channel requested, an absent bearer token is harmless —
    ``listen_env_flags`` warns and returns the base-URL flags instead of
    raising, so the test never needs a real token file.
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


def test_listen_env_flags_injects_no_testmon_var(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — fixtures supply an empty-channel config + sandboxed $HOME.
    _ = sandboxed_home
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — no testmon env var reaches ANY agent's launch env. It served a
    # hook scitex-dev's policy calls broken, its PS-HOOK-001 rule forbids, that
    # zero repos reference, and that sac itself never used.
    assert not [f for f in flags if "TESTMON" in f]


def test_listen_env_flags_injects_mcp_timeout(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — fixtures supply an empty-channel config + sandboxed $HOME.
    _ = sandboxed_home
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — the raised MCP startup connect timeout reaches the launch env
    # (fleet incident 2026-07-06 cold-start race fix).
    assert f"MCP_TIMEOUT={MCP_STARTUP_TIMEOUT_MS}" in flags


def test_listen_env_flags_mcp_timeout_follows_an_env_token(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — apptainer consumes ``--env KEY=VALUE`` as two argv tokens.
    _ = sandboxed_home
    flags = listen_env_flags(no_bus_config)
    # Act
    idx = flags.index(f"MCP_TIMEOUT={MCP_STARTUP_TIMEOUT_MS}")
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
    assert not any("GENAI_API_KEY" in f or "QWEN_API_KEY" in f for f in flags)


@pytest.fixture
def cleared_spec_dirs() -> Iterator[None]:
    """Yield with ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` UNSET, restored after."""
    prev = os.environ.get("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    try:
        yield
    finally:
        if prev is not None:
            os.environ["SCITEX_AGENT_CONTAINER_YAML_DIRS"] = prev


def test_listen_env_flags_injects_host_default_spec_dir_when_env_unset(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
    cleared_spec_dirs: None,
) -> None:
    # Arrange — host env var is unset; sandboxed $HOME roots the default.
    host_default = str((sandboxed_home / ".scitex" / "agent-container" / "agents"))
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — a YAML_DIRS --env is emitted whose value holds the host default.
    assert any(f == f"SCITEX_AGENT_CONTAINER_YAML_DIRS={host_default}" for f in flags)


def test_listen_env_flags_unions_host_set_dir_with_default(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
    cleared_spec_dirs: None,
) -> None:
    # Arrange — host sets a custom dir; the default rooted at sandboxed $HOME
    # must be unioned after it with no duplicate.
    custom = str(sandboxed_home / "custom-agents")
    os.environ["SCITEX_AGENT_CONTAINER_YAML_DIRS"] = custom
    host_default = str(sandboxed_home / ".scitex" / "agent-container" / "agents")
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert — the emitted value contains BOTH the host-set dir and default.
    assert f"SCITEX_AGENT_CONTAINER_YAML_DIRS={custom}:{host_default}" in flags


def test_listen_env_flags_injects_sac_name(sandboxed_home: Path) -> None:
    # Arrange — a config carrying the agent's own name.
    config = SimpleNamespace(name="scitex-todo", claude=SimpleNamespace(channels=[]))
    # Act
    flags = listen_env_flags(config)
    # Assert — the self-name is injected so in-container agent_list/logs +
    # spawn lineage resolve (they read SAC_NAME via _env.getenv("NAME")).
    assert "SAC_NAME=scitex-todo" in flags


def test_listen_env_flags_sac_name_follows_an_env_token(
    sandboxed_home: Path,
) -> None:
    # Arrange — apptainer consumes ``--env KEY=VALUE`` as two argv tokens.
    config = SimpleNamespace(name="scitex-todo", claude=SimpleNamespace(channels=[]))
    flags = listen_env_flags(config)
    # Act
    idx = flags.index("SAC_NAME=scitex-todo")
    # Assert
    assert flags[idx - 1] == "--env"


def test_listen_env_flags_omits_empty_sac_name(sandboxed_home: Path) -> None:
    # Arrange — an empty name must NOT be injected: an empty SAC_NAME would
    # shadow a value supplied elsewhere and is worse than absent.
    config = SimpleNamespace(name="", claude=SimpleNamespace(channels=[]))
    # Act
    flags = listen_env_flags(config)
    # Assert
    assert not any(f.startswith("SAC_NAME=") for f in flags)


def test_listen_env_flags_injects_direnv_config_location(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — every agent needs direnv's config location pointed at the base
    # image's whitelist so per-project .envrc files load without manual
    # `direnv allow`. Generic tooling knob, not coupled to any scitex-* package.
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert
    assert "DIRENV_CONFIG=/etc/direnv" in flags


def test_listen_env_flags_direnv_config_follows_an_env_token(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — apptainer consumes ``--env KEY=VALUE`` as two argv tokens.
    flags = listen_env_flags(no_bus_config)
    # Act
    idx = flags.index("DIRENV_CONFIG=/etc/direnv")
    # Assert
    assert flags[idx - 1] == "--env"


def test_listen_env_flags_injects_uv_project_environment(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — every agent's uv must default to the container-only venv
    # ``/uvwork/venv-agent`` so ad-hoc uv commands never create ``./.venv``
    # in the shared ``~/proj/<agent>`` bind (INCIDENT 2026-07-02).
    # Act
    flags = listen_env_flags(no_bus_config)
    # Assert
    assert "UV_PROJECT_ENVIRONMENT=/uvwork/venv-agent" in flags


def test_listen_env_flags_uv_project_environment_follows_an_env_token(
    no_bus_config: SimpleNamespace,
    sandboxed_home: Path,
) -> None:
    # Arrange — apptainer consumes ``--env KEY=VALUE`` as two argv tokens.
    flags = listen_env_flags(no_bus_config)
    # Act
    idx = flags.index("UV_PROJECT_ENVIRONMENT=/uvwork/venv-agent")
    # Assert
    assert flags[idx - 1] == "--env"
