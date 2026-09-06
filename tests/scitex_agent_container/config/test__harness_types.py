"""Tests for the harness axis — ``spec.harness`` + its ``spec.provider`` alias.

The axis names WHICH AGENT SDK runs the session, so it is a harness; it
used to be spelled ``spec.provider``, one word away from the unrelated
nested ``spec.claude.provider`` (an inference backend). Because the old
key is spec-facing YAML the live fleet is written in, this is a
MIGRATION, not a rename: both keys load, a stated disagreement is a hard
error, and the deprecation nudge fires on the START path.

Every load here goes through the REAL ``load_config`` against a REAL
spec.yaml on disk — the alias has to hold end to end, not just in the
resolver. STX-TQ002 AAA, STX-TQ007 one assert. No mocks.
"""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from scitex_agent_container.config import AgentConfig, load_config
from scitex_agent_container.config._explicit_validation import (
    explicit_spec_defaults,
)
from scitex_agent_container.config._harness_registry import CODEX_TUI
from scitex_agent_container.config._harness_types import (
    DEFAULT_AGENT_HARNESS,
    V4_HARNESS_DISPATCH_CARD,
    HarnessKeyConflictError,
    HarnessRuntimeMismatchError,
    declared_harness,
    ensure_harness_matches_claude_launch,
    is_known_harness,
    list_harnesses,
    resolve_spec_harness,
    uses_legacy_harness_key,
)

# ---------------------------------------------------------------------------
# Helpers — write a REAL, fully-explicit spec.yaml and load it
# ---------------------------------------------------------------------------


def _write_spec(tmp_path, name: str, overrides: dict, *, drop: tuple = ()):
    """Write ``<tmp_path>/<name>/spec.yaml`` and return its path.

    Starts from the production paste-defaults map so the spec satisfies
    the red-start explicit-fields gate, then applies ``overrides`` and
    removes ``drop`` keys — which is how a legacy-only spec is built:
    drop ``harness``, write ``provider``.
    """
    spec = explicit_spec_defaults("Agent")
    spec.update(overrides)
    for key in drop:
        spec.pop(key, None)
    spec["host"] = "${HOSTNAME}"
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": spec,
            },
            sort_keys=False,
        )
    )
    return path


def _legacy_spec(tmp_path, name: str, value: str):
    """A spec that reaches the axis ONLY through the deprecated key."""
    return _write_spec(tmp_path, name, {"provider": value}, drop=("harness",))


def _conflict(spec: dict):
    """The conflict error ``spec`` raises, or ``None`` if it raises none.

    Returning the exception (rather than asserting inside a ``raises``
    block) keeps each test to ONE assertion while still letting a second
    test interrogate the same message.
    """
    try:
        resolve_spec_harness(spec)
    except HarnessKeyConflictError as exc:
        return exc
    return None


def _load_error(path):
    """The validation error ``load_config(path)`` raises, or ``None``."""
    try:
        load_config(path)
    except ValueError as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# The resolver — key-level semantics
# ---------------------------------------------------------------------------


def test_canonical_key_resolves_to_its_value():
    # Arrange
    spec = {"harness": "openai"}
    # Act
    harness = resolve_spec_harness(spec)
    # Assert
    assert harness == "openai"


def test_legacy_key_resolves_to_the_same_value():
    # Arrange
    spec = {"provider": "openai"}
    # Act
    harness = resolve_spec_harness(spec)
    # Assert
    assert harness == "openai"


def test_neither_key_resolves_to_the_default():
    # Arrange
    spec: dict = {}
    # Act
    harness = resolve_spec_harness(spec)
    # Assert
    assert harness == DEFAULT_AGENT_HARNESS


def test_agreeing_duplicates_resolve_without_error():
    # Arrange
    spec = {"harness": "openai", "provider": "openai"}
    # Act
    harness = resolve_spec_harness(spec)
    # Assert
    assert harness == "openai"


def test_disagreeing_keys_raise():
    # Arrange
    spec = {"harness": "openai", "provider": "anthropic"}
    # Act
    error = _conflict(spec)
    # Assert
    assert isinstance(error, HarnessKeyConflictError)


def test_conflict_message_names_the_canonical_value():
    # Arrange
    spec = {"harness": "openai", "provider": "anthropic"}
    # Act
    error = _conflict(spec)
    # Assert — a message naming only one side cannot be acted on.
    assert "'openai'" in str(error)


def test_conflict_message_names_the_legacy_value():
    # Arrange
    spec = {"harness": "openai", "provider": "anthropic"}
    # Act
    error = _conflict(spec)
    # Assert
    assert "'anthropic'" in str(error)


def test_a_valueless_legacy_key_states_nothing_and_does_not_conflict():
    # Arrange — `provider:` written null satisfies an older explicit-fields
    # gate without asserting a harness, so it must not fight the new key.
    spec = {"harness": "openai", "provider": None}
    # Act
    harness = resolve_spec_harness(spec)
    # Assert
    assert harness == "openai"


def test_case_differences_are_not_a_conflict():
    # Arrange
    spec = {"harness": "Anthropic", "provider": "anthropic"}
    # Act
    harness = declared_harness(spec)
    # Assert
    assert harness == "anthropic"


def test_legacy_only_spec_is_flagged_as_legacy():
    # Arrange
    spec = {"provider": "anthropic"}
    # Act
    legacy = uses_legacy_harness_key(spec)
    # Assert
    assert legacy is True


def test_spec_carrying_both_keys_is_not_flagged_as_legacy():
    # Arrange — the author knows the new key; no nudge is owed.
    spec = {"harness": "anthropic", "provider": "anthropic"}
    # Act
    legacy = uses_legacy_harness_key(spec)
    # Assert
    assert legacy is False


def test_the_harness_registry_is_exactly_anthropic_openai_and_codex():
    # Arrange — the FAMILIES are DERIVED from config._harness_registry, so
    # "codex" appearing here is the fourth row's doing, not an edit of
    # the harness-types module (that derivation is the point).
    from scitex_agent_container.config._harness_registry import known_harnesses

    expected = {"anthropic", "openai", "codex"}
    # Act
    names = set(known_harnesses())
    # Assert
    assert names == expected


def test_list_harnesses_also_offers_the_program_name_spellings():
    """``anthropic`` is a VENDOR word standing in for a PROGRAM, and this
    axis names programs. Both spellings are accepted for the migration
    window, so the "unknown harness" error must name both — an error that
    listed only the vendor words would exclude spellings the loader
    happily accepts."""
    # Arrange
    expected = {"anthropic", "claude", "claude-code", "codex", "openai", "openai-agents"}
    # Act
    names = set(list_harnesses())
    # Assert
    assert names == expected


def test_a_program_name_spelling_resolves_to_its_family():
    # Arrange
    spec = {"harness": "claude-code"}
    # Act
    resolved = resolve_spec_harness(spec)
    # Assert
    assert resolved == "anthropic"


def test_list_harnesses_returns_sorted_order():
    # Arrange
    # Act
    names = list_harnesses()
    # Assert
    assert names == sorted(names)


def test_list_harnesses_matches_the_membership_test():
    # Arrange
    names = list_harnesses()
    # Act
    all_known = all(is_known_harness(n) for n in names)
    # Assert
    assert all_known


def test_an_unregistered_name_is_not_a_known_harness():
    # Arrange
    name = "gemini"
    # Act
    known = is_known_harness(name)
    # Assert
    assert known is False


# ---------------------------------------------------------------------------
# End to end — the same spec, written both ways, through load_config
# ---------------------------------------------------------------------------


def test_legacy_key_loads_to_the_same_harness_as_the_canonical_key(tmp_path):
    # Arrange
    old = _legacy_spec(tmp_path, "legacy-openai", "openai")
    new = _write_spec(tmp_path, "canonical-openai", {"harness": "openai"})
    # Act
    harnesses = (load_config(old).harness, load_config(new).harness)
    # Assert
    assert harnesses == ("openai", "openai")


def test_the_two_spellings_differ_in_nothing_but_the_provenance_flag(tmp_path):
    # Arrange
    old = load_config(_legacy_spec(tmp_path, "legacy-openai", "openai"))
    new = load_config(_write_spec(tmp_path, "canonical-openai", {"harness": "openai"}))
    # Two specs cannot share a directory, and the loader derives these
    # from the agent's name / path — so they differ for a reason that has
    # nothing to do with which key spelled the harness.
    ignored = {
        "name",
        "workdir",
        "screen_name",
        "config_path",
        "env",
        "hooks",
        "harness_key_is_legacy",
    }

    def fields(cfg):
        return {
            k: v for k, v in dataclasses.asdict(cfg).items() if k not in ignored
        }

    # Act
    differing = {k for k, v in fields(old).items() if fields(new)[k] != v}
    # Assert
    assert not differing


def test_the_legacy_spelling_is_recorded_as_legacy_after_load(tmp_path):
    # Arrange
    path = _legacy_spec(tmp_path, "legacy-anthropic", "anthropic")
    # Act
    config = load_config(path)
    # Assert
    assert config.harness_key_is_legacy is True


def test_the_canonical_spelling_is_not_recorded_as_legacy_after_load(tmp_path):
    # Arrange
    path = _write_spec(tmp_path, "canonical-anthropic", {"harness": "anthropic"})
    # Act
    config = load_config(path)
    # Assert
    assert config.harness_key_is_legacy is False


def test_a_legacy_only_spec_still_satisfies_the_explicit_fields_gate(tmp_path):
    # Arrange — the red-start gate must not demand BOTH spellings of one
    # declaration; a spec written before the rename has written the field.
    path = _legacy_spec(tmp_path, "legacy-explicit", "anthropic")
    # Act
    config = load_config(path)
    # Assert
    assert config.harness == "anthropic"


def test_agreeing_duplicates_load(tmp_path):
    # Arrange
    path = _write_spec(
        tmp_path, "both-agree", {"harness": "openai", "provider": "openai"}
    )
    # Act
    config = load_config(path)
    # Assert
    assert config.harness == "openai"


def test_disagreeing_duplicates_fail_the_load(tmp_path):
    # Arrange
    path = _write_spec(
        tmp_path, "both-differ", {"harness": "openai", "provider": "anthropic"}
    )
    # Act
    error = _load_error(path)
    # Assert
    assert "disagree" in str(error)


def test_the_load_error_for_a_conflict_names_the_canonical_value(tmp_path):
    # Arrange
    path = _write_spec(
        tmp_path, "both-differ", {"harness": "openai", "provider": "anthropic"}
    )
    # Act
    error = _load_error(path)
    # Assert
    assert "'openai'" in str(error)


def test_the_load_error_for_a_conflict_names_the_legacy_value(tmp_path):
    # Arrange
    path = _write_spec(
        tmp_path, "both-differ", {"harness": "openai", "provider": "anthropic"}
    )
    # Act
    error = _load_error(path)
    # Assert
    assert "'anthropic'" in str(error)


def test_a_spec_declaring_neither_key_is_still_rejected(tmp_path):
    # Arrange — accepting the alias must not weaken the red-start gate.
    path = _write_spec(tmp_path, "neither", {}, drop=("harness",))
    # Act
    error = _load_error(path)
    # Assert
    assert "harness" in str(error)


@pytest.mark.parametrize("key", ["harness", "provider"])
def test_an_illegal_value_is_rejected_in_either_spelling(tmp_path, key):
    # Arrange
    path = _write_spec(
        tmp_path,
        f"bad-{key}",
        {key: "gemini"},
        drop=("harness",) if key == "provider" else (),
    )
    # Act
    error = _load_error(path)
    # Assert
    assert f"spec.{key} must be" in str(error)


# ---------------------------------------------------------------------------
# Writers emit the canonical key
# ---------------------------------------------------------------------------


def test_the_paste_ready_defaults_write_the_canonical_key():
    # Arrange
    defaults = explicit_spec_defaults("Agent")
    # Act
    written = set(defaults) & {"harness", "provider"}
    # Assert
    assert written == {"harness"}


# ---------------------------------------------------------------------------
# The v4 step-2 loudness guard — ensure_harness_matches_claude_launch
# (card sac-v4-layering-refactor-harness-runtime-inference-20260813).
# End to end through the REAL load_config, both key spellings: the guard
# must attribute the ask to the key the spec actually used.
# ---------------------------------------------------------------------------


def _refusal(config):
    """The mismatch error the guard raises for ``config``, or ``None``."""
    try:
        ensure_harness_matches_claude_launch(
            config, launching="the Claude runner (test)"
        )
    except HarnessRuntimeMismatchError as exc:
        return exc
    return None


def test_the_guard_refuses_a_canonical_openai_spec(tmp_path):
    # Arrange
    config = load_config(_write_spec(tmp_path, "canon", {"harness": "openai"}))
    # Act
    exc = _refusal(config)
    # Assert
    assert exc is not None


def test_the_guard_names_the_canonical_key_when_the_spec_used_it(tmp_path):
    # Arrange
    config = load_config(_write_spec(tmp_path, "canon", {"harness": "openai"}))
    # Act
    exc = _refusal(config)
    # Assert
    assert exc is not None and "spec.harness" in str(exc)


def test_the_guard_refuses_a_legacy_provider_openai_spec(tmp_path):
    # Arrange — the alias the loader still accepts must reach the guard.
    config = load_config(_legacy_spec(tmp_path, "legacy", "openai"))
    # Act
    exc = _refusal(config)
    # Assert
    assert exc is not None


def test_the_guard_names_the_legacy_key_when_the_spec_used_it(tmp_path):
    # Arrange — the operator should be pointed at the line THEIR spec has.
    config = load_config(_legacy_spec(tmp_path, "legacy", "openai"))
    # Act
    exc = _refusal(config)
    # Assert
    assert exc is not None and "spec.provider" in str(exc)


def test_the_guard_names_what_was_about_to_launch(tmp_path):
    # Arrange
    config = load_config(_write_spec(tmp_path, "canon", {"harness": "openai"}))
    # Act
    exc = _refusal(config)
    # Assert
    assert exc is not None and "the Claude runner (test)" in str(exc)


def test_the_guard_names_the_v4_card(tmp_path):
    # Arrange
    config = load_config(_write_spec(tmp_path, "canon", {"harness": "openai"}))
    # Act
    exc = _refusal(config)
    # Assert
    assert exc is not None and V4_HARNESS_DISPATCH_CARD in str(exc)


def test_the_guard_passes_an_anthropic_spec_untouched(tmp_path):
    # Arrange — the fleet's entire live spec corpus (117 agent dirs on
    # this host, surveyed 2026-08-14) resolves to anthropic; the guard
    # must be a no-op for every one of them.
    config = load_config(_write_spec(tmp_path, "canon", {"harness": "anthropic"}))
    # Act
    exc = _refusal(config)
    # Assert
    assert exc is None


# ---------------------------------------------------------------------------
# The guard learns which entry the caller launches (codex-tui, 2026-09-05)
# ---------------------------------------------------------------------------


def test_guard_passes_a_codex_spec_headed_for_the_codex_tui():
    # Arrange
    config = AgentConfig(name="hm", runtime="", workdir="/tmp/hm", harness="codex")
    # Act
    outcome = ensure_harness_matches_claude_launch(
        config,
        launching="the interactive codex TUI",
        launching_key=CODEX_TUI,
        log=False,
    )
    # Assert
    assert outcome is None


def test_guard_still_refuses_a_codex_spec_headed_for_a_claude_launch():
    # Arrange -- the same spec on a Claude code path is the wrong-vendor case.
    config = AgentConfig(name="hm", runtime="", workdir="/tmp/hm", harness="codex")
    # Act
    try:
        ensure_harness_matches_claude_launch(
            config, launching="the interactive claude TUI", log=False
        )
        message = ""
    except HarnessRuntimeMismatchError as exc:
        message = str(exc)
    # Assert
    assert "codex" in message


def test_guard_refuses_an_openai_spec_even_when_told_codex_tui():
    # Arrange -- the early return is keyed on the harness AND the entry.
    config = AgentConfig(name="oa", runtime="", workdir="/tmp/oa", harness="openai")
    # Act
    try:
        ensure_harness_matches_claude_launch(
            config, launching="x", launching_key=CODEX_TUI, log=False
        )
        raised = None
    except HarnessRuntimeMismatchError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, HarnessRuntimeMismatchError)
