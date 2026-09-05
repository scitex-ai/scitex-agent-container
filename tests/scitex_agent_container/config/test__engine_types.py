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

import pytest
import yaml

from scitex_agent_container.config import load_config
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


def test_unknown_engine_error_says_the_key_is_not_declared(tmp_path):
    # Arrange
    name = "eng-unknown-msg"
    # Act
    message = _unknown_engine_message(tmp_path, name)
    # Assert
    assert "is not declared by this spec" in message


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
    # Arrange
    doc = _doc({"engines": _two_engines(default_on=None)})
    # Act
    errors = [e for e in validate_raw(doc, "spec.yaml") if "default: true" in e]
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
