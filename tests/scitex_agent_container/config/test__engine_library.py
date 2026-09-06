#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""THE THREE YAML CASES, THE ONE-LINE SWITCH, AND THE REFUSALS.

The proof the harness/engine design asked for, written to fail for the
RIGHT reason: each test asserts on the thing the operator was promised,
not on an internal shape that happens to encode it.

  * the three declared YAML cases resolve to the intended backend;
  * the one-line switch changes exactly what it claims and NOTHING else;
  * an unsupported harness x engine combination REFUSES with the
    combination NAMED in the message text;
  * the fleet default resolves from the ONE place the design names, and
    flipping that one line flips a spec that does not override it;
  * a legacy spec keeps its backend when the fleet default moves — the
    precedence that makes landing day a no-op.

NO ``monkeypatch``: the env var is set and restored by a real fixture,
and every library is a real file written to ``tmp_path``, so what these
tests exercise is the same file-reading path production takes.
"""

from __future__ import annotations

import os
import textwrap

import pytest
import yaml

from scitex_agent_container.config._engine_harness_combos import combination_verdict
from scitex_agent_container.config._engine_honour import effective_harness
from scitex_agent_container.config._engine_library import (
    FLEET_ENGINES_ENV,
    load_fleet_library,
    resolve_engine_namespace,
)
from scitex_agent_container.config._engine_precedence import resolve_default_for_spec
from scitex_agent_container.config._engine_types import apply_engine, parse_engines

FLEET_LIBRARY = """\
apiVersion: scitex-agent-container/v3
kind: EngineLibrary

engine: claude-opus

engines:
  claude-opus:
    model: opus[1m]
    provider: anthropic
  claude-haiku:
    model: haiku
    provider: anthropic
  qwen38-27b:
    model: qwen38-27b
    provider:
      base_url: http://100.64.0.1:18772
      auth_token_env: SCITEX_TEST_GATEWAY_TOKEN
    reasoning_effort: low
    max_context_tokens: 1048576
"""

QWEN_DEFAULT_LIBRARY = FLEET_LIBRARY.replace("engine: claude-opus", "engine: qwen38-27b")

# --- the three declared cases, verbatim from the design ---------------------

CASE_1_FOLLOWS_THE_FLEET = """
    spec:
      runtime: tui
      harness: anthropic
      claude:
        model: ''
        provider: null
        account: scitex-01-scitex-ai
"""

CASE_2_CODEX_ON_QWEN = """
    spec:
      runtime: tui
      harness: codex
      engine: qwen38-27b
      claude:
        model: ''
        provider: null
        account: ''
"""

CASE_3_THE_ONE_LINE_SWITCH = """
    spec:
      runtime: tui
      harness: anthropic
      engine: qwen38-27b
      claude:
        model: ''
        provider: null
        account: scitex-01-scitex-ai
"""


@pytest.fixture
def engines_file(tmp_path):
    """Write a real fleet library and point sac at it for one test.

    Each call writes a NEW file rather than rewriting one: the library
    reader memoises on ``(path, size, mtime_ns)``, and two libraries that
    differ only by an equal-length key would otherwise be a cache hit.
    """
    previous = os.environ.get(FLEET_ENGINES_ENV)
    counter = {"n": 0}

    def _write(text: str = FLEET_LIBRARY) -> str:
        counter["n"] += 1
        path = tmp_path / f"engines-{counter['n']}.yaml"
        path.write_text(text, encoding="utf-8")
        os.environ[FLEET_ENGINES_ENV] = str(path)
        return str(path)

    yield _write

    if previous is None:
        os.environ.pop(FLEET_ENGINES_ENV, None)
    else:
        os.environ[FLEET_ENGINES_ENV] = previous


def _spec(text: str) -> dict:
    return yaml.safe_load(textwrap.dedent(text))["spec"]


def _resolve(spec: dict):
    return resolve_default_for_spec(spec, resolve_engine_namespace(spec))


class _Claude:
    def __init__(self) -> None:
        self.model = "UNSET"
        self.provider = None
        self.account = "scitex-01-scitex-ai"
        self.credentials_files: list = ["a.json", "b.json"]
        self.credentials_file = "a.json"


class _Config:
    """The fields ``apply_engine`` writes, and the ones it must not."""

    def __init__(self, harness: str = "anthropic") -> None:
        self.name = "case-under-test"
        self.harness = harness
        self.runtime = "tui"
        self.claude = _Claude()
        self.env = {"KEEP": "1"}
        self.engine_key = ""
        self.reasoning_effort = ""
        self.max_context_tokens = None


# ---------------------------------------------------------------------------
# CASE 1 — a Claude-Code agent that pins nothing follows the fleet default.
# ---------------------------------------------------------------------------


def test_case_1_resolves_to_the_fleet_default_engine(engines_file):
    # Arrange
    engines_file()
    spec = _spec(CASE_1_FOLLOWS_THE_FLEET)
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine.key == "claude-opus"


def test_case_1_engine_states_no_harness_so_it_states_no_opinion(engines_file):
    # Arrange
    engines_file()
    spec = _spec(CASE_1_FOLLOWS_THE_FLEET)
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine.harness is None


def test_case_1_keeps_the_specs_own_harness(engines_file):
    # Arrange
    engines_file()
    spec = _spec(CASE_1_FOLLOWS_THE_FLEET)
    config = _Config("anthropic")
    # Act
    apply_engine(config, _resolve(spec))
    # Assert
    assert config.harness == "anthropic"


# ---------------------------------------------------------------------------
# CASE 2 — a Codex agent on Qwen: the two axes, stated independently.
# ---------------------------------------------------------------------------


def test_case_2_pins_its_engine_against_the_fleet_default(engines_file):
    # Arrange
    engines_file()
    spec = _spec(CASE_2_CODEX_ON_QWEN)
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine.key == "qwen38-27b"


def test_case_2_a_codex_harness_survives_the_engine_fold(engines_file):
    """THE PRIVILEGE FIX. The fold used to write a manufactured
    ``anthropic`` over a spec that had declared ``harness: codex``."""
    # Arrange
    engines_file()
    spec = _spec(CASE_2_CODEX_ON_QWEN)
    config = _Config("codex")
    # Act
    apply_engine(config, _resolve(spec))
    # Assert
    assert config.harness == "codex"


def test_case_2_the_effective_harness_is_the_specs_when_the_engine_is_silent(
    engines_file,
):
    # Arrange
    engines_file()
    spec = _spec(CASE_2_CODEX_ON_QWEN)
    # Act
    resolved = effective_harness(_resolve(spec), spec["harness"])
    # Assert
    assert resolved == "codex"


def test_one_engine_entry_serves_two_different_harnesses(engines_file):
    """The property that makes a FLEET library possible at all."""
    # Arrange
    engines_file()
    codex_spec = _spec(CASE_2_CODEX_ON_QWEN)
    claude_spec = _spec(CASE_3_THE_ONE_LINE_SWITCH)
    # Act
    pair = (_resolve(codex_spec).key, _resolve(claude_spec).key)
    # Assert
    assert pair == ("qwen38-27b", "qwen38-27b")


# ---------------------------------------------------------------------------
# CASE 3 — THE ONE-LINE SWITCH.
# ---------------------------------------------------------------------------


def test_the_one_line_switch_changes_the_engine(engines_file):
    # Arrange
    engines_file()
    switched = _spec(CASE_3_THE_ONE_LINE_SWITCH)
    # Act
    engine = _resolve(switched)
    # Assert
    assert engine.key == "qwen38-27b"


def test_the_one_line_switch_repoints_the_endpoint(engines_file):
    # Arrange
    engines_file()
    switched = _spec(CASE_3_THE_ONE_LINE_SWITCH)
    # Act
    engine = _resolve(switched)
    # Assert
    assert engine.provider.base_url == "http://100.64.0.1:18772"


def test_the_one_line_switch_leaves_the_harness_axis_untouched(engines_file):
    # Arrange
    engines_file()
    config = _Config("anthropic")
    # Act
    apply_engine(config, _resolve(_spec(CASE_3_THE_ONE_LINE_SWITCH)))
    # Assert
    assert config.harness == "anthropic"


def test_the_one_line_switch_leaves_the_launch_mode_untouched(engines_file):
    # Arrange
    engines_file()
    config = _Config("anthropic")
    # Act
    apply_engine(config, _resolve(_spec(CASE_3_THE_ONE_LINE_SWITCH)))
    # Assert
    assert config.runtime == "tui"


def test_the_one_line_switch_leaves_unrelated_env_untouched(engines_file):
    # Arrange
    engines_file()
    config = _Config("anthropic")
    # Act
    apply_engine(config, _resolve(_spec(CASE_3_THE_ONE_LINE_SWITCH)))
    # Assert
    assert config.env["KEEP"] == "1"


def test_a_provider_backed_engine_clears_the_oauth_credentials_pool(engines_file):
    """The rule the runtime enforces, made true of what actually RUNS."""
    # Arrange
    engines_file()
    config = _Config("anthropic")
    # Act
    apply_engine(config, _resolve(_spec(CASE_3_THE_ONE_LINE_SWITCH)))
    # Assert
    assert config.claude.credentials_files == []


def test_a_provider_backed_engine_clears_the_oauth_account(engines_file):
    # Arrange
    engines_file()
    config = _Config("anthropic")
    # Act
    apply_engine(config, _resolve(_spec(CASE_3_THE_ONE_LINE_SWITCH)))
    # Assert
    assert config.claude.account == ""


# ---------------------------------------------------------------------------
# THE FLEET DEFAULT — one line, one place.
# ---------------------------------------------------------------------------


def test_flipping_the_fleet_default_flips_an_unpinned_spec(engines_file):
    # Arrange
    engines_file(QWEN_DEFAULT_LIBRARY)
    spec = _spec(CASE_1_FOLLOWS_THE_FLEET)
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine.key == "qwen38-27b"


def test_the_unflipped_fleet_default_leaves_the_same_spec_on_claude(engines_file):
    # Arrange
    engines_file(FLEET_LIBRARY)
    spec = _spec(CASE_1_FOLLOWS_THE_FLEET)
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine.key == "claude-opus"


def test_flipping_the_fleet_default_does_not_move_a_pinned_spec(engines_file):
    # Arrange
    engines_file(QWEN_DEFAULT_LIBRARY)
    spec = _spec(
        """
        spec:
          runtime: tui
          harness: anthropic
          engine: claude-haiku
          claude:
            model: ''
            provider: null
        """
    )
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine.key == "claude-haiku"


def test_a_legacy_backend_declaration_outranks_the_fleet_default(engines_file):
    """THE LOAD-BEARING PRECEDENCE: on landing day, nobody moves."""
    # Arrange
    engines_file(QWEN_DEFAULT_LIBRARY)
    spec = _spec(
        """
        spec:
          runtime: tui
          harness: anthropic
          claude:
            model: opus[1m]
            provider: null
        """
    )
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine is None


def test_a_spec_local_engine_wins_a_fleet_name_collision(engines_file):
    # Arrange
    engines_file()
    spec = _spec(
        """
        spec:
          runtime: tui
          harness: anthropic
          engine: qwen38-27b
          engines:
            qwen38-27b:
              model: qwen38-27b
              provider:
                base_url: http://127.0.0.1:19999
                auth_token_env: SCITEX_TEST_GATEWAY_TOKEN
          claude:
            model: ''
            provider: null
        """
    )
    # Act
    engine = _resolve(spec)
    # Assert
    assert engine.provider.base_url == "http://127.0.0.1:19999"


def test_a_missing_fleet_library_is_legal_and_declares_no_engines(engines_file):
    # Arrange
    written = engines_file()
    os.environ[FLEET_ENGINES_ENV] = written + ".absent"
    # Act
    library = load_fleet_library()
    # Assert
    assert (library.exists, library.engines, library.errors) == (False, {}, ())


def test_a_fleet_default_naming_nothing_is_reported_not_swallowed(engines_file):
    # Arrange
    engines_file(FLEET_LIBRARY.replace("engine: claude-opus", "engine: typo-here"))
    # Act
    library = load_fleet_library()
    # Assert
    assert any("typo-here" in message for message in library.errors)


def test_an_unreadable_fleet_library_is_reported_not_treated_as_absent(engines_file):
    # Arrange
    engines_file("engines:\n  broken: [\n")
    # Act
    library = load_fleet_library()
    # Assert
    assert library.errors


# ---------------------------------------------------------------------------
# REFUSALS — the COMBINATION is named, at start time, not deep in argv build.
# ---------------------------------------------------------------------------


def _engine(**entry):
    return parse_engines({"engines": {"under-test": entry}})["under-test"]


def test_codex_with_a_providerless_engine_is_not_honourable():
    # Arrange
    engine = _engine(model="opus")
    # Act
    verdict = combination_verdict(engine, "codex")
    # Assert
    assert verdict.refuses


def test_codex_with_a_providerless_engine_names_the_harness_in_the_reason():
    # Arrange
    engine = _engine(model="opus")
    # Act
    verdict = combination_verdict(engine, "codex")
    # Assert
    assert "codex" in verdict.reason


def test_codex_with_a_providerless_engine_names_the_engine_in_the_reason():
    # Arrange
    engine = _engine(model="opus")
    # Act
    verdict = combination_verdict(engine, "codex")
    # Assert
    assert "under-test" in verdict.reason


def test_openai_harness_refuses_every_engine_naming_the_missing_adapter():
    # Arrange
    engine = _engine(model="gpt", provider="anthropic")
    # Act
    verdict = combination_verdict(engine, "openai")
    # Assert
    assert "no lifecycle launch adapter" in verdict.reason


def test_codex_harness_over_the_codex_provider_is_refused_as_a_name_clash():
    # Arrange
    engine = _engine(model="gpt", provider="codex")
    # Act
    verdict = combination_verdict(engine, "codex")
    # Assert
    assert "name clash" in verdict.reason


def test_an_undetermined_harness_is_could_not_tell_never_honourable():
    # Arrange
    engine = _engine(model="m", provider="anthropic")
    # Act
    verdict = combination_verdict(engine, None)
    # Assert
    assert verdict.undetermined


def test_codex_with_a_provider_bearing_engine_is_honourable(engines_file):
    # Arrange
    engines_file()
    engine = _resolve(_spec(CASE_2_CODEX_ON_QWEN))
    # Act
    verdict = combination_verdict(engine, "codex")
    # Assert
    assert verdict.honourable


def test_anthropic_with_a_providerless_oauth_engine_is_honourable():
    # Arrange
    engine = _engine(model="opus[1m]")
    # Act
    verdict = combination_verdict(engine, "anthropic")
    # Assert
    assert verdict.honourable
