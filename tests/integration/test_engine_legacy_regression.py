#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AN OLD SPEC MUST STILL PARSE IDENTICALLY — over REAL tracked specs.

The harness/engine split changes how the engine is CHOSEN, and the whole
claim of the design is that day-one blast radius is ZERO: every one of
the deployed specs still carries a legacy backend declaration, that
declaration is a PIN, and a pin outranks the fleet default. A synthetic
fixture cannot test that claim, because a synthetic fixture is written by
the same person who wrote the resolver. So the two fixtures here are
BYTE COPIES of live tracked specs from the fleet:

  ``legacy_tracked_spec.yaml``  — the majority shape: no ``engines:``
      block at all, the backend stated in ``spec.claude.model``.
  ``engines_tracked_spec.yaml`` — the ONE live spec already written on
      the multi-engine surface, with a ``default: true`` marker and a
      provider-backed Qwen entry.

Each is loaded TWICE: once with no fleet engine library on the host (the
state of every host today) and once with a library whose fleet default is
a DIFFERENT backend. The two loads must agree. If they ever stop
agreeing, a fleet-file edit has silently repointed an agent, which is the
exact failure the precedence exists to prevent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.config._engine_library import FLEET_ENGINES_ENV

FIXTURES = Path(__file__).parent / "fixtures"

LIBRARY_DEFAULTING_TO_QWEN = """\
apiVersion: scitex-agent-container/v3
kind: EngineLibrary

engine: fleet-qwen

engines:
  fleet-qwen:
    model: fleet-qwen-model
    provider:
      base_url: http://127.0.0.1:18772
      auth_token_env: SCITEX_TEST_GATEWAY_TOKEN
  fleet-claude:
    model: fleet-claude-model
    provider: anthropic
"""


@pytest.fixture
def without_a_fleet_library():
    """The state of every host today: no library, nothing to follow."""
    previous = os.environ.get(FLEET_ENGINES_ENV)
    os.environ[FLEET_ENGINES_ENV] = "/nonexistent/engines.yaml"
    yield
    if previous is None:
        os.environ.pop(FLEET_ENGINES_ENV, None)
    else:
        os.environ[FLEET_ENGINES_ENV] = previous


@pytest.fixture
def with_a_fleet_library_defaulting_elsewhere(tmp_path):
    """A real library on disk whose fleet default is a DIFFERENT backend."""
    previous = os.environ.get(FLEET_ENGINES_ENV)
    path = tmp_path / "engines.yaml"
    path.write_text(LIBRARY_DEFAULTING_TO_QWEN, encoding="utf-8")
    os.environ[FLEET_ENGINES_ENV] = str(path)
    yield path
    if previous is None:
        os.environ.pop(FLEET_ENGINES_ENV, None)
    else:
        os.environ[FLEET_ENGINES_ENV] = previous


def _backend(spec_name: str):
    """The backend triple a real tracked spec resolves to."""
    config = load_config(str(FIXTURES / spec_name))
    claude = config.claude
    provider = getattr(claude, "provider", None)
    return (
        config.harness,
        getattr(claude, "model", ""),
        str(getattr(provider, "base_url", "") or ""),
    )


# ---------------------------------------------------------------------------
# The MAJORITY shape — a legacy single-backend spec.
# ---------------------------------------------------------------------------


def test_a_legacy_tracked_spec_loads_without_a_fleet_library(
    without_a_fleet_library,
):
    # Arrange
    name = "legacy_tracked_spec.yaml"
    # Act
    harness, model, _endpoint = _backend(name)
    # Assert
    assert (harness, model) == ("anthropic", "opus[1m]")


def test_a_legacy_tracked_spec_ignores_a_fleet_default_naming_another_backend(
    with_a_fleet_library_defaulting_elsewhere,
):
    """THE LOAD-BEARING CLAIM: writing the fleet library moves nobody."""
    # Arrange
    name = "legacy_tracked_spec.yaml"
    # Act
    harness, model, _endpoint = _backend(name)
    # Assert
    assert (harness, model) == ("anthropic", "opus[1m]")


def test_a_legacy_tracked_spec_keeps_its_empty_endpoint_under_a_fleet_library(
    with_a_fleet_library_defaulting_elsewhere,
):
    # Arrange
    name = "legacy_tracked_spec.yaml"
    # Act
    _harness, _model, endpoint = _backend(name)
    # Assert
    assert endpoint == ""


# ---------------------------------------------------------------------------
# The ONE live spec already on the multi-engine surface.
# ---------------------------------------------------------------------------


def test_an_engines_tracked_spec_keeps_its_marked_default_without_a_library(
    without_a_fleet_library,
):
    # Arrange
    name = "engines_tracked_spec.yaml"
    # Act
    _harness, model, _endpoint = _backend(name)
    # Assert
    assert model == "qwen38-27b"


def test_an_engines_tracked_spec_keeps_its_marked_default_under_a_fleet_library(
    with_a_fleet_library_defaulting_elsewhere,
):
    # Arrange
    name = "engines_tracked_spec.yaml"
    # Act
    _harness, model, _endpoint = _backend(name)
    # Assert
    assert model == "qwen38-27b"


def test_an_engines_tracked_spec_keeps_its_own_endpoint_under_a_fleet_library(
    with_a_fleet_library_defaulting_elsewhere,
):
    # Arrange
    name = "engines_tracked_spec.yaml"
    # Act
    _harness, _model, endpoint = _backend(name)
    # Assert
    assert endpoint == "http://100.64.0.1:18772"


def test_an_engines_tracked_spec_keeps_the_harness_its_spec_declares(
    without_a_fleet_library,
):
    """Its Qwen entry states no harness, so the SPEC's ``anthropic`` stands."""
    # Arrange
    name = "engines_tracked_spec.yaml"
    # Act
    harness, _model, _endpoint = _backend(name)
    # Assert
    assert harness == "anthropic"
