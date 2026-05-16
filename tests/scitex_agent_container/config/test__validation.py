"""Tests for scitex_agent_container.config._validation.

Coverage:
- F-CS7: ``spec.model`` is validated against accepted SDK aliases /
  versioned forms at yaml-validate time. Bad strings (e.g. the
  abbreviated ``claude-opus[1m]`` which silently fails inside the
  SDK) must be rejected with a clear error pointing at the canonical
  forms.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._validation import validate_raw

_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {
        "runtime": "apptainer",
    },
}


def _spec(model):
    # v3-realign: spec.model moved to spec.claude.model.
    return {**_BASE, "spec": {**_BASE["spec"], "claude": {"model": model}}}


@pytest.mark.parametrize(
    "model",
    [
        "opus",
        "sonnet",
        "haiku",
        "inherit",
        "default",
        "opus[1m]",
        "sonnet[1m]",
        "claude-opus-4-7",
        "claude-opus-4-7[1m]",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    ],
)
def test_valid_models_pass(model):
    """Aliases and full versioned forms must validate cleanly."""
    # Arrange
    raw = _spec(model)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    bad = [e for e in errors if "spec.claude.model" in e]
    assert bad == [], f"unexpected spec.model errors for {model!r}: {bad}"


_INVALID_MODELS = [
    "claude-opus[1m]",  # the F-CS7 reproducer — abbreviated, no version
    "claude-opus",
    "claude-sonnet",
    "claude-haiku",
    "opusx",
    "claude-foo-1-2",  # unknown family
]


@pytest.mark.parametrize("model", _INVALID_MODELS)
def test_invalid_models_are_rejected(model):
    """Abbreviated / unknown forms must fail validation."""
    # Arrange
    raw = _spec(model)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    bad = [e for e in errors if "spec.claude.model" in e]
    assert bad, f"expected spec.model rejection for {model!r}, got none"


@pytest.mark.parametrize("model", _INVALID_MODELS)
def test_invalid_model_error_echoes_offending_string(model):
    """The rejection message must echo the offending model string."""
    # Arrange
    raw = _spec(model)
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.model" in e]
    # Assert
    assert model in bad[0], "error must echo the offending model string"


@pytest.mark.parametrize("model", _INVALID_MODELS)
def test_invalid_model_error_points_at_canonical_form(model):
    """The rejection message must redirect users to canonical forms."""
    # Arrange
    raw = _spec(model)
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.model" in e]
    # Assert
    assert "claude-opus-4-7" in bad[0] or "alias" in bad[0].lower(), (
        "error must point the user at the canonical forms"
    )


def test_missing_model_is_allowed():
    """Empty / missing model is fine — runtime falls back to its default."""
    # Arrange
    raw = _BASE
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.claude.model" in e]


def test_non_string_model_is_rejected():
    """Numbers, lists, nulls etc. must be rejected."""
    # Arrange
    raw = _spec(42)
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.model" in e]


def test_non_string_model_error_mentions_string_type():
    """The rejection message for a non-string model must call out the type."""
    # Arrange
    raw = _spec(42)
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.claude.model" in e]
    # Assert
    assert "string" in bad[0].lower()


# ---------------------------------------------------------------------------
# F-CS6 — yaml-field rename: spec.runtime soft alias
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_runtime_warning_marker(tmp_path):
    """Each test gets its own XDG_RUNTIME_DIR so the once-per-shell
    marker file doesn't leak warnings between cases. Explicit env
    save/restore — no monkeypatch (PA-306)."""
    import os

    saved = os.environ.get("XDG_RUNTIME_DIR")
    os.environ["XDG_RUNTIME_DIR"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = saved


@pytest.mark.parametrize("alias", ["claude-cli-tui", "claude-sdk-persistent"])
def test_validate_raw_rejects_f_cs6_aliases_after_f_cs17(alias):
    """F-CS17 stage 2 rejects every legacy / aliased runtime value.
    F-CS6's yaml-friendly aliases (claude-cli-tui /
    claude-sdk-persistent) hard-error alongside their canonical
    forms — sac is container-only."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": alias},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("spec.runtime" in e and alias in e for e in errors), (
        f"alias {alias!r} must hard-error with a redirect"
    )


def test_validate_raw_rejects_unknown_runtime_value():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "claude-xtreme"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("spec.runtime" in e for e in errors)


# ---------------------------------------------------------------------------
# F-CS3 — autonomous spec block (phase 1: schema only)
# ---------------------------------------------------------------------------


def _autonomous_spec(autonomous):
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer", "autonomous": autonomous},
    }


def test_autonomous_default_block_passes_validation():
    # Arrange
    raw = _autonomous_spec(
        {
            "enabled": True,
            "drive_until": "DONE",
            "max_turns": 50,
            "idle_kick_after_s": 120,
            "kick_text": "Continue. Print DONE when finished.",
        }
    )
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.autonomous" in e]


def test_autonomous_block_is_optional():
    """No autonomous block at all is fine — defaults apply at parse time."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.autonomous" in e]


def test_autonomous_block_must_be_a_mapping():
    # Arrange
    raw = _autonomous_spec("not-a-dict")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("spec.autonomous must be a mapping" in e for e in errors)


def test_autonomous_drive_until_rejects_empty_string():
    # Arrange
    raw = _autonomous_spec({"drive_until": ""})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("drive_until" in e and "non-empty" in e for e in errors)


def test_autonomous_drive_until_rejects_non_string():
    # Arrange
    raw = _autonomous_spec({"drive_until": 42})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("drive_until must be a string" in e for e in errors)


def test_autonomous_max_turns_rejects_zero():
    # Arrange
    raw = _autonomous_spec({"max_turns": 0})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("max_turns must be > 0" in e for e in errors)


def test_autonomous_max_turns_rejects_non_integer_string():
    # Arrange
    raw = _autonomous_spec({"max_turns": "fifty"})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("max_turns must be an integer" in e for e in errors)


def test_autonomous_max_turns_rejects_boolean():
    """bool is a subclass of int — still rejected."""
    # Arrange
    raw = _autonomous_spec({"max_turns": True})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("max_turns must be an integer" in e for e in errors)


def test_autonomous_idle_kick_must_be_positive_int():
    # Arrange
    raw = _autonomous_spec({"idle_kick_after_s": -1})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("idle_kick_after_s must be > 0" in e for e in errors)


def test_autonomous_enabled_must_be_bool():
    # Arrange
    raw = _autonomous_spec({"enabled": "yes"})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("enabled must be a boolean" in e for e in errors)


def test_autonomous_parse_returns_defaults_when_missing():
    from scitex_agent_container.config._parsers import parse_autonomous
    from scitex_agent_container.config._types import AutonomousSpec

    # Arrange
    raw = {}
    # Act
    out = parse_autonomous(raw)
    # Assert
    assert out == AutonomousSpec()


@pytest.fixture
def _parsed_full_autonomous_block():
    from scitex_agent_container.config._parsers import parse_autonomous

    # Arrange
    raw = {
        "autonomous": {
            "enabled": True,
            "drive_until": "ALL DONE",
            "max_turns": 7,
            "idle_kick_after_s": 60,
            "kick_text": "keep going",
        }
    }
    # Act
    return parse_autonomous(raw)


def test_autonomous_parse_reads_enabled(_parsed_full_autonomous_block):
    # Arrange
    out = _parsed_full_autonomous_block
    # Act
    value = out.enabled
    # Assert
    assert value is True


def test_autonomous_parse_reads_drive_until(_parsed_full_autonomous_block):
    # Arrange
    out = _parsed_full_autonomous_block
    # Act
    value = out.drive_until
    # Assert
    assert value == "ALL DONE"


def test_autonomous_parse_reads_max_turns(_parsed_full_autonomous_block):
    # Arrange
    out = _parsed_full_autonomous_block
    # Act
    value = out.max_turns
    # Assert
    assert value == 7


def test_autonomous_parse_reads_idle_kick_after_s(_parsed_full_autonomous_block):
    # Arrange
    out = _parsed_full_autonomous_block
    # Act
    value = out.idle_kick_after_s
    # Assert
    assert value == 60


def test_autonomous_parse_reads_kick_text(_parsed_full_autonomous_block):
    # Arrange
    out = _parsed_full_autonomous_block
    # Act
    value = out.kick_text
    # Assert
    assert value == "keep going"


# ---------------------------------------------------------------------------
# F-CS16 phase 2a — schema flatten: spec.image, spec.dockerfile, runtime
# ---------------------------------------------------------------------------


def _spec_with(extra: dict):
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer", **extra},
    }


def test_validate_raw_accepts_apptainer_runtime():
    """`apptainer` is the only accepted runtime since 2026-05-13."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.runtime" in e]


_LEGACY_RUNTIMES = [
    "docker",
    "podman",
    "claude-code",
    "claude-cli-tui",
    "claude-session",
    "claude-sdk-persistent",
    "slurm",
    "slurm-tenant",
]


@pytest.mark.parametrize("legacy", _LEGACY_RUNTIMES)
def test_validate_raw_rejects_legacy_runtime_values(legacy):
    """Every non-apptainer runtime is rejected at parse time
    (docker / podman dropped 2026-05-13; legacy SDK strings even
    earlier)."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": legacy},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    runtime_errors = [e for e in errors if "spec.runtime" in e]
    assert runtime_errors, f"legacy runtime {legacy!r} must be rejected"


@pytest.mark.parametrize("legacy", _LEGACY_RUNTIMES)
def test_legacy_runtime_rejection_echoes_offending_value(legacy):
    """The rejection message for a legacy runtime must echo the offending value."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": legacy},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    runtime_errors = [e for e in errors if "spec.runtime" in e]
    # Assert
    assert legacy in runtime_errors[0]


def test_validate_raw_accepts_apptainer_image_field():
    # Arrange — v3-realign: image moved into spec.apptainer.image.
    raw = _spec_with({"apptainer": {"image": "scitex-agent-container:scitex"}})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.apptainer.image" in e]


def test_validate_raw_rejects_top_level_image_field():
    """v3-realign: top-level spec.image is rejected with a relocation hint."""
    # Arrange
    raw = _spec_with({"image": "scitex-agent-container:scitex"})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("spec.image" in e and "spec.apptainer.image" in e for e in errors)


def test_validate_raw_rejects_non_string_image():
    # Arrange
    raw = _spec_with({"apptainer": {"image": 42}})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("spec.apptainer.image" in e and "string" in e for e in errors)


def test_validate_raw_rejects_non_string_dockerfile():
    """``spec.dockerfile`` is no longer interpreted (docker ripout
    2026-05-13), but a non-string value still surfaces a type error
    so an operator can't put a list there by accident."""
    # Arrange
    raw = _spec_with({"dockerfile": ["./a", "./b"]})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("spec.dockerfile" in e and "string" in e for e in errors)


def test_image_defaults_to_empty_in_agentconfig(tmp_path):
    """A yaml without ``image`` must still parse; AgentConfig.image
    stays empty so the apptainer dispatcher applies its default
    (the sac-scitex SIF) at dispatch."""
    import yaml as _yaml

    from scitex_agent_container.config import load_config

    # Arrange
    yaml_dir = tmp_path / "image-default"
    yaml_dir.mkdir()
    yaml_path = yaml_dir / "image-default.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": {"runtime": "apptainer"},
            }
        )
    )
    # Act
    cfg = load_config(str(yaml_path))
    # Assert
    assert cfg.image == ""


@pytest.fixture
def _loaded_config_with_image(tmp_path):
    import yaml as _yaml

    from scitex_agent_container.config import load_config

    # Arrange
    yaml_dir = tmp_path / "image-set"
    yaml_dir.mkdir()
    yaml_path = yaml_dir / "image-set.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": {
                    "runtime": "apptainer",
                    "apptainer": {
                        "image": "~/.scitex/agent-container/containers/sac-scitex.sif",
                    },
                },
            }
        )
    )
    # Act
    return load_config(str(yaml_path))


def test_image_round_trips_into_apptainer_image(_loaded_config_with_image):
    # Arrange
    cfg = _loaded_config_with_image
    # Act
    value = cfg.apptainer.image
    # Assert — v3: AgentConfig.apptainer.image is populated from yaml.
    assert value == "~/.scitex/agent-container/containers/sac-scitex.sif"


def test_image_round_trips_into_top_level_image_alias(_loaded_config_with_image):
    # Arrange
    cfg = _loaded_config_with_image
    # Act
    value = cfg.image
    # Assert — v3: AgentConfig.image mirrors spec.apptainer.image.
    assert value == "~/.scitex/agent-container/containers/sac-scitex.sif"


# ---------------------------------------------------------------------------
# spec.remote — rejection error message must point at spec.host (not orochi)
# ---------------------------------------------------------------------------


def _remote_errors():
    """Validate a spec with the removed ``spec.remote`` field; return errors."""
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer", "remote": {"host": "nas"}},
    }
    errors = validate_raw(raw, path="<test>")
    return [e for e in errors if "spec.remote" in e or "remote" in e.lower()]


def test_spec_remote_rejection_mentions_spec_host():
    # Arrange
    bad = _remote_errors()
    # Act
    message = bad[0] if bad else ""
    # Assert — the rejection redirects users to the v3 replacement field.
    assert "spec.host" in message, (
        f"spec.remote rejection must redirect to spec.host; got {message!r}"
    )


def test_spec_remote_rejection_does_not_blame_orochi():
    # Arrange
    bad = _remote_errors()
    # Act
    message = bad[0] if bad else ""
    # Assert — sac v3 supports cross-host natively; the error must not
    # send users to orochi.
    assert "orochi" not in message.lower(), (
        f"spec.remote rejection must not mention orochi; got {message!r}"
    )


def test_spec_remote_rejection_drops_stale_section_reference():
    # Arrange
    bad = _remote_errors()
    # Act
    message = bad[0] if bad else ""
    # Assert — the old "§2" pointer is stale and should not appear.
    assert "§2" not in message, (
        f"spec.remote rejection must not cite stale §2; got {message!r}"
    )
