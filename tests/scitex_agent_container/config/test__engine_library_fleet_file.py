#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""THE TRACKED FLEET ENGINE LIBRARY, read through the production reader.

``.scitex/agent-container/engines.yaml`` is the file `sac agents
migrate-engines` stopped copying into 119 specs: the ``qwen38-27b`` engine is
declared THERE, once. A tracked YAML blob nothing loads is a blob that rots,
so every assertion below goes through :func:`load_fleet_library` — the same
call the loader makes — against the real file on disk.

WHY A SEPARATE MODULE from ``test__engine_library``: that one is at the .py
line cap, and its subject is the library MECHANISM (three YAML cases, the
one-line switch, the refusals) written against fixtures in ``tmp_path``. This
one's subject is the one library THIS REPO SHIPS.

NO MOCKS, NO ``monkeypatch``: the path override is set and restored by a real
fixture, and the file under test is the tracked file itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.config._engine_library import (
    FLEET_ENGINES_ENV,
    FLEET_ENGINES_FILENAME,
    fleet_engines_path,
    load_fleet_library,
)
from scitex_agent_container.config._qwen_gateway import (
    QWEN_GATEWAY_HOST,
    QWEN_GATEWAY_PROVIDER,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The tracked library, at the path the module docstring commits to.
TRACKED_LIBRARY = (
    _REPO_ROOT / ".scitex" / "agent-container" / FLEET_ENGINES_FILENAME
)

#: The gateway engine's key. A LITERAL, because the point of this file is
#: that the key is DATA now — deriving it from the code under test would make
#: the assertion true by construction.
FLEET_QWEN_KEY = "qwen38-27b"


@pytest.fixture
def env_override():
    """Set ``$SAC_ENGINES_FILE`` for one test and restore it afterwards."""
    previous = os.environ.get(FLEET_ENGINES_ENV)

    def _set(value: "str | None") -> None:
        if value is None:
            os.environ.pop(FLEET_ENGINES_ENV, None)
        else:
            os.environ[FLEET_ENGINES_ENV] = value

    yield _set
    _set(previous)


@pytest.fixture
def library(env_override):
    """The tracked file, loaded through the production reader."""
    env_override(str(TRACKED_LIBRARY))
    return load_fleet_library()


# ---------------------------------------------------------------------------
# The file exists where the docs say, and parses through the real reader
# ---------------------------------------------------------------------------


def test_the_tracked_library_is_where_the_docs_say_it_is() -> None:
    # Arrange — docs/harness-and-engine.md links this exact path.
    path = TRACKED_LIBRARY
    # Act
    found = path.is_file()
    # Assert
    assert found, f"the tracked fleet engine library is missing at {path}"


def test_the_tracked_library_parses_without_errors(library) -> None:
    # Arrange — a malformed library is a LOAD error for every spec that
    # depends on it, so this file is checked rather than trusted.
    # Act
    errors = library.errors
    # Assert
    assert errors == ()


def test_the_tracked_library_declares_the_gateway_engine(library) -> None:
    # Arrange — the entry the sweep used to copy into every spec.
    # Act
    keys = sorted(library.engines)
    # Assert
    assert keys == [FLEET_QWEN_KEY]


# ---------------------------------------------------------------------------
# What the entry says — and the two things it deliberately does not
# ---------------------------------------------------------------------------


def test_the_gateway_engine_names_its_provider_rather_than_an_address(
    library,
) -> None:
    # Arrange — one address, in one module, not copied into YAML.
    # Act
    engine = library.engines[FLEET_QWEN_KEY]
    # Assert
    assert engine.provider_declared == QWEN_GATEWAY_PROVIDER


def test_the_gateway_engine_still_resolves_to_an_endpoint(library) -> None:
    # Arrange — naming it by reference must not mean naming nothing.
    # Act
    engine = library.engines[FLEET_QWEN_KEY]
    # Assert
    assert engine.provider is not None and engine.provider.base_url


def test_the_gateway_address_is_not_written_into_the_library() -> None:
    # Arrange — the positive control for the test above: the endpoint
    # resolves, and the hostname is nowhere in the file.
    text = TRACKED_LIBRARY.read_text(encoding="utf-8")
    # Act
    written = QWEN_GATEWAY_HOST in text
    # Assert
    assert written is False


def test_the_gateway_engine_states_no_harness(library) -> None:
    # Arrange — stating one would claim the HARNESS axis, and this ONE entry
    # has to serve a Claude-Code agent and a Codex agent unchanged.
    # Act
    engine = library.engines[FLEET_QWEN_KEY]
    # Assert
    assert engine.harness is None


def test_the_gateway_engine_runs_at_low_reasoning_effort(library) -> None:
    # Arrange — Q4 (operator, 2026-09-03): permanently low.
    # Act
    engine = library.engines[FLEET_QWEN_KEY]
    # Assert
    assert engine.reasoning_effort == "low"


def test_the_gateway_engine_carries_the_measured_context_window(library) -> None:
    # Arrange — the gateway's serve conf MAX_MODEL_LEN. Claude Code assumes
    # 200k for a model it does not recognise and compacts at that boundary.
    # Act
    engine = library.engines[FLEET_QWEN_KEY]
    # Assert
    assert engine.max_context_tokens == 1048576


def test_the_tracked_library_declares_no_fleet_default(library) -> None:
    # Arrange — writing `engine:` here repoints every unpinned agent, which
    # is the operator's one-line decision and not the migration's to make.
    # Act
    default_key = library.default_key
    # Assert
    assert default_key == ""


# ---------------------------------------------------------------------------
# How it is FOUND — stated, not assumed
# ---------------------------------------------------------------------------


def test_the_override_env_var_decides_the_path_when_set(env_override) -> None:
    # Arrange — the ops/test seam, and the one this module's fixture uses.
    env_override(str(TRACKED_LIBRARY))
    # Act
    resolved = fleet_engines_path()
    # Assert
    assert resolved == TRACKED_LIBRARY


def test_with_no_override_the_path_sits_beside_the_agents_dir(
    env_override,
) -> None:
    # Arrange — the fleet case: $SAC_ENGINES_FILE unset, so the library is
    # resolved from the state root and lands next to `agents/`. Asserted
    # against the SSOT resolver rather than a hard-coded ~/.scitex, so a host
    # exporting $SCITEX_DIR is answered correctly too.
    from scitex_agent_container._state.state_paths import agent_container_root

    env_override(None)
    # Act
    resolved = fleet_engines_path()
    # Assert
    assert resolved == agent_container_root() / FLEET_ENGINES_FILENAME
