"""Selected harness requirements through the real v3 config load boundary."""

from __future__ import annotations

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.config._explicit_validation import validate
from scitex_agent_container.config._loaders import load_v3
from tests.scitex_agent_container.config.test__explicit_validation import (
    _explicit_doc,
    _raised_by,
    _remove,
    _write_doc,
)

_CLAUDE_FIELDS = (
    "model",
    "channels",
    "flags",
    "raw_options",
    "session",
    "continue_max_age_minutes",
    "resume_id",
    "auto_accept",
    "account",
    "credentials_file",
    "credentials_files",
    "provider",
)
_NON_CLAUDE = ("hermes", "codex", "opencode", "openai-agents")


def _selected_doc(harness: str | None, *, runtime: str = "tui") -> dict:
    """Use the independent hand-authored green fixture, then remove Claude."""
    doc = _explicit_doc()
    spec = doc["spec"]
    del spec["provider"]
    del spec["claude"]
    spec.update(harness=harness, runtime=runtime)
    return doc


def _load_error(tmp_path, doc: dict) -> str:
    path = _write_doc(tmp_path, doc)
    with pytest.raises(ValueError) as error:
        load_config(path)
    return str(error.value)


def _canonical_doc(harness: str) -> dict:
    doc = _selected_doc(harness)
    spec = doc["spec"]
    del spec["container"]
    del spec["watchdog"]
    spec["comms"]["channels"] = []
    entry = {"session": {"mode": "continue", "max_age_minutes": None}}
    if harness == "codex":
        entry.update(approval_policy="never", sandbox_mode="danger-full-access")
    spec["available_harnesses"] = {harness: entry}
    return doc


@pytest.mark.parametrize("harness", _NON_CLAUDE)
def test_non_claude_spec_loads_without_a_claude_block(tmp_path, harness):
    # Arrange
    path = _write_doc(tmp_path, _selected_doc(harness))
    # Act
    config = load_config(path)
    # Assert
    assert config.harness == ("openai" if harness == "openai-agents" else harness)


@pytest.mark.parametrize("harness", _NON_CLAUDE)
def test_direct_v3_loader_accepts_non_claude_spec(tmp_path, harness):
    # Arrange
    doc = _selected_doc(harness)
    path = _write_doc(tmp_path, doc)
    # Act
    config = load_v3(doc, path)
    # Assert
    assert config.harness == ("openai" if harness == "openai-agents" else harness)


def test_native_codex_headless_config_has_no_claude_requirement(tmp_path):
    # Arrange
    path = _write_doc(tmp_path, _selected_doc("codex", runtime="headless"))
    # Act
    config = load_config(path)
    # Assert
    assert (config.harness, config.runtime) == ("codex", "headless")


@pytest.mark.parametrize("harness", _NON_CLAUDE)
def test_legacy_provider_alias_selects_the_same_non_claude_requirement(
    tmp_path, harness
):
    # Arrange
    doc = _selected_doc(harness)
    doc["spec"]["provider"] = doc["spec"].pop("harness")
    path = _write_doc(tmp_path, doc)
    # Act
    config = load_config(path)
    # Assert
    assert config.harness == ("openai" if harness == "openai-agents" else harness)


@pytest.mark.parametrize("harness", ("anthropic", "claude", "claude-code"))
@pytest.mark.parametrize("field", _CLAUDE_FIELDS)
def test_claude_still_requires_each_explicit_field(tmp_path, harness, field):
    # Arrange
    doc = _explicit_doc()
    doc["spec"]["provider"] = harness
    del doc["spec"]["claude"][field]
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert f"spec.claude.{field} —" in error


@pytest.mark.parametrize("harness", (None, "", "   "))
def test_unstated_harness_keeps_claude_requirements(tmp_path, harness):
    # Arrange
    doc = _selected_doc(harness)
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert "12 required field(s)" in error


@pytest.mark.parametrize("harness", _NON_CLAUDE)
@pytest.mark.parametrize("field", ("runtime", "workdir", "a2a.port", "apptainer.binds"))
def test_non_claude_still_requires_genuine_common_fields(tmp_path, harness, field):
    # Arrange
    doc = _remove(_selected_doc(harness), field)
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert f"spec.{field} —" in error


@pytest.mark.parametrize("harness", _NON_CLAUDE)
def test_minimal_non_claude_config_stays_red_without_vendor_paste_fields(
    tmp_path, harness
):
    # Arrange
    doc = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"harness": harness, "runtime": "tui", "host": "${HOSTNAME}"},
    }
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert "spec.workdir —" in error and "spec.claude." not in error


@pytest.mark.parametrize("harness", ("hermes", "codex", "opencode"))
def test_canonical_selected_session_reaches_typed_config(tmp_path, harness):
    # Arrange
    path = _write_doc(tmp_path, _canonical_doc(harness))
    # Act
    config = load_config(path)
    # Assert
    assert (config.harness, config.claude.session) == (harness, "continue")


@pytest.mark.parametrize(
    ("harness", "field"),
    (("codex", "approval_policy"), ("codex", "sandbox_mode"), ("hermes", "session")),
)
def test_selected_harness_required_fields_remain_load_errors(tmp_path, harness, field):
    # Arrange
    doc = _canonical_doc(harness)
    del doc["spec"]["available_harnesses"][harness][field]
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert f"spec.available_harnesses.{harness} is missing required fields" in error


@pytest.mark.parametrize("harness", ("hermes", "codex", "opencode"))
@pytest.mark.parametrize("field", ("mode", "max_age_minutes"))
def test_selected_session_cannot_borrow_an_omitted_field(tmp_path, harness, field):
    # Arrange
    doc = _canonical_doc(harness)
    del doc["spec"]["available_harnesses"][harness]["session"][field]
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert f"session is missing required fields: ['{field}']" in error


@pytest.mark.parametrize("harness", ("hermes", "codex"))
def test_undeclared_selected_harness_is_not_satisfied_by_another_entry(
    tmp_path, harness
):
    # Arrange
    doc = _canonical_doc(harness)
    doc["spec"]["available_harnesses"] = {
        "opencode": {"session": {"mode": "continue", "max_age_minutes": None}}
    }
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert f"selects '{harness}', but spec.available_harnesses does not declare it" in error


def test_conflicting_harness_aliases_still_refuse(tmp_path):
    # Arrange
    doc = _selected_doc("hermes")
    doc["spec"]["provider"] = "anthropic"
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert "spec.harness='hermes' and spec.provider='anthropic' disagree" in error


def test_unknown_harness_still_refuses(tmp_path):
    # Arrange
    doc = _selected_doc("hermes-typo")
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert "spec.harness must be one of" in error


@pytest.mark.parametrize("harness", _NON_CLAUDE)
def test_declared_legacy_claude_auth_conflict_is_not_hidden(tmp_path, harness):
    # Arrange
    doc = _selected_doc(harness)
    doc["spec"]["claude"] = {
        "account": "synthetic-account",
        "provider": {
            "name": "synthetic",
            "base_url": "http://127.0.0.1:1",
            "api_key_env": "SYNTHETIC_KEY",
        },
    }
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert "spec.claude.provider and spec.claude.account are mutually exclusive" in error


def test_native_subscription_engine_still_requires_an_account_pin(tmp_path):
    # Arrange
    doc = _selected_doc("codex")
    doc["spec"].update(
        engine="native",
        available_engines={"native": {"subscription": {"provider": "openai"}}},
    )
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert "subscription.account must name a collected, qualified OpenAI account" in error


def test_non_claude_cannot_name_an_undeclared_engine(tmp_path):
    # Arrange
    doc = _selected_doc("hermes")
    doc["spec"]["engine"] = "undeclared-synthetic"
    # Act
    error = _load_error(tmp_path, doc)
    # Assert
    assert "names an engine nothing declares" in error


def test_explicit_gate_has_no_harness_bypass_for_missing_common_fields(tmp_path):
    # Arrange
    doc = _remove(_selected_doc("hermes"), "health.method")
    # Act
    error = _raised_by(lambda: validate(doc, tmp_path / "spec.yaml"))
    # Assert
    assert "spec.health.method —" in str(error)
