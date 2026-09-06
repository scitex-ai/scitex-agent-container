"""Tests for ``spec.engines`` — several declared backends, ONE picked at start.

Every load here goes through the REAL ``load_config`` against a REAL
spec.yaml on disk (or the REAL ``validate_raw`` against a real doc): the
engine axis has to hold end to end, not just in the resolver. No mocks
of the thing under test.

POSITIVE CONTROLS are explicit throughout, because most of these
assertions are about something NOT happening (no fallback, no error, no
change) and an assertion about an absence passes just as well when the
mechanism is missing entirely. So each "refuses" test is paired with the
same spec that DOES resolve, and each "unchanged" test asserts the
resolved value rather than merely the absence of an error.
"""

from __future__ import annotations

import os

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._engine_library import FLEET_ENGINES_ENV
from scitex_agent_container.config._engine_types import (
    EngineDefaultError,
    UnknownEngineError,
    apply_engine,
    default_engine,
    legacy_conflict_messages,
    parse_engines,
    select_engine,
)
from scitex_agent_container.config._explicit_validation import (
    explicit_spec_defaults,
)
from scitex_agent_container.config._validation import validate_raw
from tests.scitex_agent_container._helpers.explicit_spec import deep_merge

_QWEN_PROVIDER = {
    "base_url": "http://127.0.0.1:18772",
    "auth_token_env": "SAC_TEST_QWEN_KEY",
}


@pytest.fixture(autouse=True)
def _no_fleet_engine_library(tmp_path_factory):
    """Pin the fleet library at a path that does not exist, for every test here.

    HERMETICITY, not decoration. Default resolution now walks the
    precedence chain in ``_engine_precedence.resolve_default_for_spec``,
    whose last step calls ``_engine_library.fleet_default_key()`` — and
    that reads ``$SAC_ENGINES_FILE`` (else
    ``$SCITEX_DIR/agent-container/engines.yaml``) from the REAL
    environment of whatever host runs the suite. Nothing else in this
    module pins it, so without this fixture a developer machine that
    happens to carry a fleet ``engines.yaml`` gets different answers from
    one that does not. Every spec in this file declares its own engines
    and resolves at an EARLIER precedence step, so the library must
    contribute nothing here — pointing it at a nonexistent file is how we
    assert that rather than assume it.
    """
    missing = tmp_path_factory.mktemp("no-fleet-library") / "engines.yaml"
    previous = os.environ.get(FLEET_ENGINES_ENV)
    os.environ[FLEET_ENGINES_ENV] = str(missing)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(FLEET_ENGINES_ENV, None)
        else:
            os.environ[FLEET_ENGINES_ENV] = previous


def _doc(overrides: dict, *, kind: str = "Agent") -> dict:
    # deep_merge, NOT dict.update: ``spec.claude`` is a REQUIRED mapping
    # with eleven explicit keys, and a shallow update would replace the
    # whole block with the one key a test cares about — turning every
    # legacy-block test into an explicit-spec failure instead of the
    # migration assertion it is meant to be.
    spec = deep_merge(explicit_spec_defaults(kind), overrides)
    spec["host"] = "${HOSTNAME}"
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": kind,
        "spec": spec,
    }


def _write(tmp_path, name: str, overrides: dict) -> str:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(_doc(overrides), sort_keys=False))
    return str(path)


def _two_engines(*, default_on: str | None = "claude") -> dict:
    engines = {
        "claude": {"harness": "anthropic", "model": "fable[1m]"},
        "qwen38-27b": {
            "harness": "anthropic",
            "model": "qwen38-27b",
            "provider": dict(_QWEN_PROVIDER),
            "reasoning_effort": "low",
            "max_context_tokens": 393216,
        },
    }
    if default_on is not None:
        engines[default_on]["default"] = True
    return engines


# ---------------------------------------------------------------------------
# Single default resolution
# ---------------------------------------------------------------------------


def test_single_declared_engine_is_the_default_without_a_default_marker():
    # Arrange
    engines = parse_engines({"engines": {"solo": {"model": "fable[1m]"}}})
    # Act
    selected = default_engine(engines)
    # Assert
    assert selected.key == "solo"


def test_declared_default_engine_resolves_onto_the_loaded_config(tmp_path):
    # Arrange
    path = _write(tmp_path, "eng-default", {"engines": _two_engines()})
    # Act
    config = load_config(path)
    # Assert
    assert (config.engine_key, config.claude.model) == ("claude", "fable[1m]")


def test_default_engine_leaves_the_non_default_provider_unapplied(tmp_path):
    # POSITIVE CONTROL for the selection test below: with no --engine the
    # qwen provider must NOT be in play, so the later assertion that it IS
    # in play proves selection, not merely that the spec mentions qwen.
    # Arrange
    path = _write(tmp_path, "eng-default-prov", {"engines": _two_engines()})
    # Act
    config = load_config(path)
    # Assert
    assert config.claude.provider is None


# ---------------------------------------------------------------------------
# Explicit --engine selection
# ---------------------------------------------------------------------------


def test_explicit_engine_selection_picks_the_named_entry(tmp_path):
    # Arrange
    path = _write(tmp_path, "eng-pick", {"engines": _two_engines()})
    config = load_config(path)
    # Act
    selected = select_engine(config.engines, "qwen38-27b")
    # Assert
    assert selected.model == "qwen38-27b"


def test_explicit_engine_selection_carries_its_provider(tmp_path):
    # Arrange
    path = _write(tmp_path, "eng-pick-prov", {"engines": _two_engines()})
    config = load_config(path)
    # Act
    selected = select_engine(config.engines, "qwen38-27b")
    # Assert
    assert selected.provider.base_url == _QWEN_PROVIDER["base_url"]


def test_no_requested_engine_falls_back_to_the_declared_default(tmp_path):
    # POSITIVE CONTROL: the same call with no key must land on the default,
    # so the test above is measuring the KEY and not the parser.
    # Arrange
    path = _write(tmp_path, "eng-pick-none", {"engines": _two_engines()})
    config = load_config(path)
    # Act
    selected = select_engine(config.engines, None)
    # Assert
    assert selected.key == "claude"


# ---------------------------------------------------------------------------
# Unknown --engine key
# ---------------------------------------------------------------------------


def _unknown_engine_message(tmp_path, name: str) -> str:
    """The error text ``--engine gpt-9`` produces against a two-engine spec."""
    path = _write(tmp_path, name, {"engines": _two_engines()})
    config = load_config(path)
    try:
        select_engine(config.engines, "gpt-9")
    except UnknownEngineError as exc:
        return str(exc)
    return ""


def test_unknown_engine_key_raises_rather_than_using_the_default(tmp_path):
    # Arrange
    path = _write(tmp_path, "eng-unknown", {"engines": _two_engines()})
    config = load_config(path)
    # Act
    act = lambda: select_engine(config.engines, "gpt-9")  # noqa: E731
    # Assert
    with pytest.raises(UnknownEngineError):
        act()


def test_unknown_engine_error_says_the_key_resolves_to_no_engine(tmp_path):
    """The message widened when the FLEET engine library became a second
    source of keys: "not declared by this spec" was true and unhelpful
    once a key could equally belong in the library."""
    # Arrange
    name = "eng-unknown-msg"
    # Act
    message = _unknown_engine_message(tmp_path, name)
    # Assert
    assert "resolves to no engine" in message


def test_unknown_engine_error_names_the_fleet_library_as_a_source(tmp_path):
    # Arrange
    name = "eng-unknown-src"
    # Act
    message = _unknown_engine_message(tmp_path, name)
    # Assert
    assert "fleet" in message


def test_unknown_engine_error_lists_the_declared_keys(tmp_path):
    # Arrange
    name = "eng-unknown-list"
    # Act
    message = _unknown_engine_message(tmp_path, name)
    # Assert
    assert "'claude'" in message and "'qwen38-27b'" in message


# ---------------------------------------------------------------------------
# Default-marker arithmetic
# ---------------------------------------------------------------------------


def test_two_defaults_is_a_hard_error_naming_both(tmp_path):
    # Arrange
    engines = _two_engines()
    engines["qwen38-27b"]["default"] = True
    doc = _doc({"engines": engines})
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "default" in e]
    # Assert
    assert errors and "'claude'" in errors[0] and "'qwen38-27b'" in errors[0]


def test_exactly_one_default_produces_no_default_error():
    # POSITIVE CONTROL for the two-defaults test: the same shape with one
    # marker must be clean, so the error above is about the COUNT.
    # Arrange
    doc = _doc({"engines": _two_engines()})
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "default" in e]
    # Assert
    assert errors == []


def test_two_engines_with_no_default_is_a_hard_error():
    """sac does not pick: taking the first would make the backend depend
    on YAML ordering. The REMEDY the message names changed from
    ``default: true`` on an entry to one ``engine:`` line at the top of
    spec:, so the assertion follows the remedy, not the old spelling."""
    # Arrange
    doc = _doc({"engines": _two_engines(default_on=None)})
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "engine: <key>" in e]
    # Assert
    assert errors


def test_default_engine_raises_for_two_marked_defaults():
    # Arrange
    engines = _two_engines()
    engines["qwen38-27b"]["default"] = True
    parsed = parse_engines({"engines": engines})
    # Act
    act = lambda: default_engine(parsed)  # noqa: E731
    # Assert
    with pytest.raises(EngineDefaultError):
        act()


# ---------------------------------------------------------------------------
# MIGRATION — legacy-only, both-agreeing, both-disagreeing
# ---------------------------------------------------------------------------


def test_legacy_only_spec_loads_with_no_engines(tmp_path):
    # Arrange
    path = _write(
        tmp_path,
        "legacy-only",
        {"harness": "anthropic", "claude": {"model": "fable[1m]"}},
    )
    # Act
    config = load_config(path)
    # Assert
    assert config.engines == {}


def test_legacy_only_spec_keeps_its_model_unchanged(tmp_path):
    # Arrange
    path = _write(
        tmp_path,
        "legacy-only-model",
        {"harness": "anthropic", "claude": {"model": "fable[1m]"}},
    )
    # Act
    config = load_config(path)
    # Assert
    assert config.claude.model == "fable[1m]"


def test_legacy_only_spec_produces_no_engine_errors(tmp_path):
    # Arrange
    doc = _doc({"harness": "anthropic", "claude": {"model": "fable[1m]"}})
    # Act
    errors = validate_raw(doc, "spec.yaml")
    # Assert
    assert errors == []


def test_legacy_block_agreeing_with_the_default_engine_is_accepted():
    # Arrange
    doc = _doc(
        {
            "harness": "anthropic",
            "claude": {"model": "fable[1m]"},
            "engines": _two_engines(),
        }
    )
    # Act
    errors = validate_raw(doc, "spec.yaml")
    # Assert
    assert errors == []


def test_legacy_block_agreeing_still_resolves_to_the_default_engine(tmp_path):
    # POSITIVE CONTROL: "accepted" must not mean "ignored" — the engine is
    # still the thing that resolves.
    # Arrange
    path = _write(
        tmp_path,
        "both-agree",
        {
            "harness": "anthropic",
            "claude": {"model": "fable[1m]"},
            "engines": _two_engines(),
        },
    )
    # Act
    config = load_config(path)
    # Assert
    assert config.engine_key == "claude"


def test_legacy_model_disagreeing_with_the_default_engine_is_a_hard_error():
    # Arrange
    doc = _doc(
        {
            "harness": "anthropic",
            "claude": {"model": "sonnet"},
            "engines": _two_engines(),
        }
    )
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "disagree" in e]
    # Assert
    assert errors


def test_the_disagreement_error_names_both_values():
    # Arrange
    doc = _doc(
        {
            "harness": "anthropic",
            "claude": {"model": "sonnet"},
            "engines": _two_engines(),
        }
    )
    # Act
    message = next(e for e in validate_raw(doc, "spec.yaml") if "disagree" in e)
    # Assert
    assert "'sonnet'" in message and "'fable[1m]'" in message


def test_an_empty_legacy_model_states_no_opinion_and_never_conflicts():
    # POSITIVE CONTROL for the disagreement rule: written-but-empty is not
    # a claim, so it must not manufacture a conflict.
    # Arrange
    spec = {
        "harness": "anthropic",
        "claude": {"model": ""},
        "engines": _two_engines(),
    }
    # Act
    messages = legacy_conflict_messages(spec)
    # Assert
    assert messages == []


def test_a_disagreeing_provider_between_the_blocks_is_reported():
    # Arrange
    engines = _two_engines(default_on="qwen38-27b")
    spec = {
        "harness": "anthropic",
        "claude": {"model": "qwen38-27b", "provider": "deepseek"},
        "engines": engines,
    }
    # Act
    messages = legacy_conflict_messages(spec)
    # Assert
    assert any("provider" in m for m in messages)


def test_the_same_provider_spelled_as_a_name_and_a_dict_agrees():
    # POSITIVE CONTROL for provider comparison: a registry NAME and a dict
    # copy-pasted from that registry entry are ONE backend, so comparing
    # them as strings would produce a false conflict.
    # Arrange
    spec = {
        "harness": "anthropic",
        "claude": {
            "model": "deepseek-chat",
            "provider": {
                "base_url": "https://api.deepseek.com/anthropic",
                "auth_token_env": "DEEPSEEK_API_KEY",
            },
        },
        "engines": {
            "ds": {"model": "deepseek-chat", "provider": "deepseek"},
        },
    }
    # Act
    messages = legacy_conflict_messages(spec)
    # Assert
    assert messages == []


# ---------------------------------------------------------------------------
# Shape / coupling
# ---------------------------------------------------------------------------


def test_engines_on_an_agentproxy_spec_is_rejected():
    # Arrange
    doc = _doc(
        {"proxy": {"upstream": "http://127.0.0.1:9000"}, "engines": _two_engines()},
        kind="AgentProxy",
    )
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "spec.engines" in e]
    # Assert
    assert errors


def test_an_unknown_field_inside_an_engine_entry_is_rejected():
    # Arrange
    doc = _doc({"engines": {"solo": {"model": "fable[1m]", "temprature": 1}}})
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "unknown field" in e]
    # Assert
    assert errors


def test_an_unknown_harness_inside_an_engine_entry_is_rejected():
    # Arrange
    doc = _doc({"engines": {"solo": {"harness": "gemini"}}})
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "harness" in e]
    # Assert
    assert errors


def test_a_known_harness_inside_an_engine_entry_is_accepted():
    # POSITIVE CONTROL: the harness check must resolve through the real
    # registry, not reject every value.
    # Arrange
    doc = _doc({"engines": {"solo": {"harness": "anthropic"}}})
    # Act
    errors = validate_raw(doc, "spec.yaml")
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# A provider-backed engine clears the OAuth account the default engine pins.
# Measured 2026-09-05: `business --engine qwen38-27b` was refused at runtime
# ("provider and account are mutually exclusive") while its spec was correct,
# because the fold left spec.claude.account in place.
# ---------------------------------------------------------------------------
def test_selecting_a_provider_engine_clears_the_oauth_account(tmp_path):
    # Arrange
    path = _write(
        tmp_path,
        "eng-prov-acct",
        {"engines": _two_engines(), "claude": {"account": "acct-a"}},
    )
    config = load_config(path)
    # Act
    apply_engine(config, select_engine(config.engines, "qwen38-27b"))
    # Assert
    assert config.claude.account == ""


def test_selecting_the_oauth_engine_keeps_the_account(tmp_path):
    # Arrange
    path = _write(
        tmp_path,
        "eng-oauth-acct",
        {"engines": _two_engines(), "claude": {"account": "acct-a"}},
    )
    config = load_config(path)
    # Act
    apply_engine(config, select_engine(config.engines, "claude"))
    # Assert
    assert config.claude.account == "acct-a"


# ---------------------------------------------------------------------------
# The engine fold reaches the TOP-LEVEL model surface too
# ---------------------------------------------------------------------------

# The shape the migration writes, and the one the operator's hand-migrated
# `business` spec already had: the engines carry the model and
# `spec.claude.model` states nothing. `_loaders` derives AgentConfig.model
# and the injected SCITEX_AGENT_CONTAINER_MODEL from that RAW field, so
# without the fold both fall to the "sonnet" default while the agent runs
# something else. Measured over the 119-spec corpus: 117 flipped.
_MIGRATED = {
    "claude": {"model": "", "provider": None},
    "engines": {"claude": {"harness": "anthropic", "model": "opus[1m]", "default": True}},
}


def test_the_default_engines_model_becomes_the_top_level_model(tmp_path):
    # Arrange — `sac agents list` and the tmux runner both read this field.
    path = _write(tmp_path, "eng-model-surface", _MIGRATED)
    # Act
    config = load_config(path)
    # Assert
    assert config.model == "opus[1m]"


def test_the_default_engines_model_becomes_the_injected_model_env(tmp_path):
    # Arrange — injected into every container; `sac whoami` prints it.
    path = _write(tmp_path, "eng-model-env", _MIGRATED)
    # Act
    config = load_config(path)
    # Assert
    assert config.env["SCITEX_AGENT_CONTAINER_MODEL"] == "Claude Opus (1M)"


def test_a_selected_engines_model_becomes_the_top_level_model(tmp_path):
    # Arrange — the same must hold for `--engine <key>` at start, not only
    # for the load-time default fold.
    path = _write(tmp_path, "eng-model-selected", {"engines": _two_engines()})
    config = load_config(path)
    # Act
    apply_engine(config, select_engine(config.engines, "qwen38-27b"))
    # Assert
    assert config.model == "qwen38-27b"


def test_a_selected_engines_model_becomes_the_injected_model_env(tmp_path):
    # Arrange
    path = _write(tmp_path, "eng-env-selected", {"engines": _two_engines()})
    config = load_config(path)
    # Act
    apply_engine(config, select_engine(config.engines, "qwen38-27b"))
    # Assert
    assert config.env["SCITEX_AGENT_CONTAINER_MODEL"] == "qwen38-27b"


def test_an_engine_stating_no_model_falls_back_to_the_documented_default(tmp_path):
    # Arrange — the positive control for the fold's own default: "" means
    # "use the runtime default", which is the same `sonnet` the legacy read
    # applies, not an empty model string.
    path = _write(
        tmp_path,
        "eng-model-unstated",
        {"claude": {"model": ""}, "engines": {"solo": {"harness": "anthropic"}}},
    )
    # Act
    config = load_config(path)
    # Assert
    assert config.model == "sonnet"


def test_an_explicit_env_declaration_still_wins_over_the_fold(tmp_path):
    # Arrange — the loader's rule is that user values override auto-derived
    # ones, and refreshing the DERIVED half must not overrule an author.
    path = _write(
        tmp_path,
        "eng-model-env-declared",
        deep_merge(
            _MIGRATED,
            {"apptainer": {"env": {"SCITEX_AGENT_CONTAINER_MODEL": "hand-written"}}},
        ),
    )
    # Act
    config = load_config(path)
    # Assert
    assert config.env["SCITEX_AGENT_CONTAINER_MODEL"] == "hand-written"


# ---------------------------------------------------------------------------
# THE TWO AXES DO NOT TOUCH. An entry that states no harness states no
# opinion about the harness, and the model fold happens anyway. This is the
# fleet-library entry shape — one definition serving a Claude-Code agent and
# a Codex agent unchanged — and every other engine test in this file states
# `harness: anthropic`, so without these two the harness-less shape is
# untested here.
# ---------------------------------------------------------------------------

_HARNESSLESS = {
    "harness": "codex",
    "claude": {"model": "", "provider": None},
    "engines": {"solo": {"model": "opus[1m]"}},
}


def test_an_engine_stating_no_harness_leaves_the_specs_declared_harness(tmp_path):
    # Arrange — the regression the nullable field exists to prevent: the fold
    # used to write a manufactured "anthropic" over a spec that said `codex`.
    path = _write(tmp_path, "eng-harnessless-axis", _HARNESSLESS)
    # Act
    config = load_config(path)
    # Assert
    assert config.harness == "codex"


def test_an_engine_stating_no_harness_still_folds_its_model(tmp_path):
    # Arrange — the positive control for the test above: leaving the harness
    # alone must not mean leaving the ENTRY alone.
    path = _write(tmp_path, "eng-harnessless-model", _HARNESSLESS)
    # Act
    config = load_config(path)
    # Assert
    assert config.model == "opus[1m]"


def test_an_engine_that_does_state_a_harness_still_writes_it(tmp_path):
    # Arrange — POSITIVE CONTROL for "leaves the declared harness alone":
    # `codex` surviving proves nothing unless a STATED harness demonstrably
    # replaces it through the SAME branch. It has to be applied rather than
    # loaded: a spec whose legacy `harness:` disagrees with its DEFAULT
    # engine's is a hard load error (`legacy_conflict_messages`), so the only
    # way an entry's harness differs from the spec's is `--engine <key>`
    # picking a NON-default entry at start.
    path = _write(tmp_path, "eng-harness-stated", _HARNESSLESS)
    config = load_config(path)
    other = parse_engines({"engines": {"other": {"harness": "anthropic"}}})
    # Act
    apply_engine(config, select_engine(other, "other"))
    # Assert
    assert config.harness == "anthropic"


# ---------------------------------------------------------------------------
# ONE FOLD, BOTH AXES — the merge guard. `apply_engine` carries two
# independently-developed changes on the same few lines (the model surface,
# and clearing the OAuth rotation pool for a provider-backed engine).
# Resolving that region by taking either side wholesale loses one of them,
# and losing the pool-clearing half is SILENT unless something asserts both
# after the SAME call. These two do.
# ---------------------------------------------------------------------------


def test_a_provider_engine_clears_the_whole_rotation_pool(tmp_path):
    # Arrange — a pool beside a provider-backed engine composed an API-key
    # provider with OAuth credentials at launch; validation never saw it,
    # because it asserts the exclusion on the RAW spec, not the folded config.
    path = _write(
        tmp_path,
        "eng-pool-cleared",
        {"engines": _two_engines(), "claude": {"credentials_files": ["a.json"]}},
    )
    config = load_config(path)
    # Act
    apply_engine(config, select_engine(config.engines, "qwen38-27b"))
    # Assert
    assert config.claude.credentials_files == []


def test_an_oauth_engine_leaves_the_rotation_pool_populated(tmp_path):
    # Arrange — POSITIVE CONTROL for the test above. `credentials_files == []`
    # would also pass if the loader simply never carried the pool onto the
    # config, so the pool has to be shown SURVIVING an engine that declares
    # no provider before its clearing means anything.
    path = _write(
        tmp_path,
        "eng-pool-kept",
        {"engines": _two_engines(), "claude": {"credentials_files": ["a.json"]}},
    )
    config = load_config(path)
    # Act
    apply_engine(config, select_engine(config.engines, "claude"))
    # Assert
    assert config.claude.credentials_files == ["a.json"]


def test_the_same_fold_also_writes_the_top_level_model(tmp_path):
    # Arrange — the other half of the same statement, asserted after the same
    # call so a one-sided resolution of that region cannot pass both.
    path = _write(
        tmp_path,
        "eng-pool-cleared-model",
        {"engines": _two_engines(), "claude": {"credentials_files": ["a.json"]}},
    )
    config = load_config(path)
    # Act
    apply_engine(config, select_engine(config.engines, "qwen38-27b"))
    # Assert
    assert config.model == "qwen38-27b"
