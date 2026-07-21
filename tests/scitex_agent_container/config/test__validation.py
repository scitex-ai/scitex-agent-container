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
        # Fable family added 2026-06-12 (lead msg 0bde9c41). Fable
        # ships as a 1-digit family version (``claude-fable-5``); the
        # ``[1m]`` suffix is the CLI-native 1M-context tier selector,
        # empirically verified against SDK 0.2.87 / CLI 2.1.150 to
        # round-trip with model_usage[claude-fable-5[1m]].contextWindow=1M.
        "fable",
        "fable[1m]",
        "claude-fable-5",
        "claude-fable-5[1m]",
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
    "claude-fable",  # abbreviated — Fable still requires the digit
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


def test_missing_model_is_rejected():
    """Missing model is REQUIRED now — no hidden default (operator 2026-06-23)."""
    # Arrange
    raw = _BASE
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.claude.model" in e]


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
# spec.runtime — repurposed to launch-mode (operator directive 12870)
# ---------------------------------------------------------------------------


def test_validate_raw_accepts_runtime_claude_agent_sdk():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "claude-agent-sdk"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.runtime" in e]


def test_validate_raw_accepts_runtime_tui():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "tui"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.runtime" in e]


def test_validate_raw_accepts_runtime_apptainer_for_backcompat():
    # Arrange — pre-2026-06-13 corpus must keep validating.
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.runtime" in e]


# ---------------------------------------------------------------------------
# spec.provider — agent SDK family selector (openai-compat-1 foundation).
# Distinct from spec.claude.provider (vendor backend override, tested in
# test__validation_provider.py) — see the naming-collision note in
# config._provider_types.AgentProvider.
# ---------------------------------------------------------------------------


def test_validate_raw_absent_provider_adds_no_value_error():
    # Arrange — the VALUE check stays a no-op when spec.provider is absent
    # (the red-start ruling flags the MISSING field separately; the
    # "must be one of" value diagnostic must not fire on absence).
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "tui"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.provider must be one of" in e]


def test_validate_raw_absent_provider_is_flagged_missing():
    # Arrange — red-start ruling 2026-07-21: every field explicit.
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "tui"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.provider" in e]


def test_validate_raw_accepts_provider_anthropic():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "tui", "provider": "anthropic"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.provider" in e]


def test_validate_raw_accepts_provider_openai():
    # Arrange — schema-valid even though openai-compat-2 hasn't landed
    # a runner for it yet (foundation phase).
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "tui", "provider": "openai"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.provider" in e]


def test_validate_raw_rejects_unknown_provider_value():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "tui", "provider": "bogus-sdk"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.provider" in e]


def test_validate_raw_unknown_provider_error_echoes_offending_value():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "tui", "provider": "bogus-sdk"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    provider_errors = [e for e in errors if "spec.provider" in e]
    assert "bogus-sdk" in provider_errors[0]


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


def test_autonomous_block_absent_is_flagged_missing():
    """Red-start ruling 2026-07-21: the autonomous block must be written."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.autonomous.enabled" in e]


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


def test_image_defaults_to_empty_in_agentconfig():
    """AgentConfig.image keeps its empty dataclass default for INTERNAL
    construction, so the apptainer dispatcher applies its default SIF.

    Author YAML must now declare ``apptainer.image`` (the validator's
    required-field check covers that); the RETAINED dataclass default still
    backs programmatic construction, which is what this asserts directly."""
    from scitex_agent_container.config._types import AgentConfig

    # Arrange
    name = "image-default"
    # Act
    cfg = AgentConfig(name=name)
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
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_spec,
    )

    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": explicit_spec(
                    {
                        "runtime": "apptainer",
                        "host": "${HOSTNAME}",
                        "workdir": "/home/agent/work",
                        "apptainer": {
                            "image": (
                                "~/.scitex/agent-container/containers/sac-scitex.sif"
                            ),
                            "binds": [],
                        },
                        "claude": {"model": "claude-opus-4-8[1m]"},
                        "health": {"enabled": True, "interval": 60},
                        "restart": {"policy": "on-failure", "max_retries": 3},
                    }
                ),
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
# Required author fields — NO HIDDEN DEFAULTS (operator directive 2026-06-23).
# Every APPLICABLE field must be declared; the validator errors roundly when
# one is absent. ``host: ${HOSTNAME}`` resolves to the loading machine
# (``host: local`` is BANNED; operator directive 2026-07-10).
# ---------------------------------------------------------------------------


def _complete_spec() -> dict:
    """Fully-explicit spec (red-start ruling 2026-07-21: EVERY field)."""
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_spec,
    )

    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": explicit_spec(
            {
                "runtime": "tui",
                "host": "${HOSTNAME}",
                "workdir": "/home/agent/work",
                "apptainer": {"image": "/x.sif", "binds": []},
                "claude": {"model": "opus"},
                "health": {"enabled": True, "interval": 60},
                "restart": {"policy": "on-failure", "max_retries": 3},
            }
        ),
    }


_COMPLETE_SPEC = _complete_spec()


def _spec_without(dotted_path: str) -> dict:
    """Deep-copy ``_COMPLETE_SPEC`` with one ``spec.<dotted_path>`` removed."""
    import copy

    raw = copy.deepcopy(_COMPLETE_SPEC)
    parts = dotted_path.split(".")
    cur = raw["spec"]
    for part in parts[:-1]:
        cur = cur[part]
    cur.pop(parts[-1], None)
    return raw


def test_complete_spec_has_no_errors():
    # Arrange
    raw = _COMPLETE_SPEC
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert errors == []


def test_host_local_passes_validation():
    # Arrange
    raw = _COMPLETE_SPEC
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "host" in e.lower()]


def test_complete_spec_with_explicit_provider_openai_has_no_errors():
    # Arrange — a fully-valid spec that ALSO opts into the (not-yet-
    # runnable) openai SDK family must still validate clean.
    import copy

    raw = copy.deepcopy(_COMPLETE_SPEC)
    raw["spec"]["provider"] = "openai"
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert errors == []


def test_missing_host_and_hosts_is_rejected():
    # Arrange
    raw = _spec_without("host")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.host or spec.hosts is REQUIRED" in e]


def test_missing_workdir_is_rejected():
    # Arrange
    raw = _spec_without("workdir")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.workdir" in e]


def test_missing_apptainer_image_is_rejected():
    # Arrange
    raw = _spec_without("apptainer.image")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.apptainer.image" in e]


def test_missing_apptainer_binds_is_rejected():
    # Arrange
    raw = _spec_without("apptainer.binds")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.apptainer.binds" in e]


def test_missing_health_interval_is_rejected():
    # Arrange
    raw = _spec_without("health.interval")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.health.interval" in e]


def test_missing_restart_policy_is_rejected():
    # Arrange
    raw = _spec_without("restart.policy")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.restart.policy" in e]


def test_agentproxy_missing_upstream_is_rejected():
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "AgentProxy",
        "spec": {
            "runtime": "tui",
            "host": "${HOSTNAME}",
            "workdir": "/work",
            "apptainer": {"image": "/x.sif", "binds": []},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "always", "max_retries": 5},
            "proxy": {"trust": "untrusted"},
        },
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.proxy.upstream" in e]


def test_multi_host_requires_workdir_key_too():
    # Arrange — red-start ruling 2026-07-21: EVERY field is written, multi-
    # host included. (The old multi-host exemption is gone; a multi-instance
    # spec writes ``workdir: null`` to keep the per-instance derivation.)
    import copy

    raw = copy.deepcopy(_COMPLETE_SPEC)
    raw["spec"].pop("workdir")
    raw["spec"].pop("host")
    raw["spec"]["hosts"] = "all"
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.workdir" in e]


def test_multi_host_null_workdir_keeps_derivation_green():
    # Arrange — ``workdir: null`` is the explicit spelling of "derive the
    # per-instance runtime workdir" (present-but-null counts as declared).
    import copy

    raw = copy.deepcopy(_COMPLETE_SPEC)
    raw["spec"]["workdir"] = None
    raw["spec"].pop("host")
    raw["spec"]["hosts"] = "all"
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.workdir" in e]


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


# ---------------------------------------------------------------------------
# Unknown spec field — generic catch-all for typos / undeclared keys.
#
# The validator must reject any spec key outside ``_KNOWN_SPEC_KEYS`` plus
# the v3-relocated/removed sets. This guards against typos silently
# disappearing into ``spec.extensions`` semantics, and keeps the docs and
# validator in lockstep — every example in the skills must use only known
# field names.
# ---------------------------------------------------------------------------


def test_validate_raw_rejects_unknown_spec_field():
    """An undeclared top-level spec key is rejected with a helpful error."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer", "totally_made_up_field": 42},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert — the rejection names the offending key AND points at the
    # canonical escape hatch (spec.extensions for custom data).
    assert any(
        "totally_made_up_field" in e and "spec.extensions" in e for e in errors
    ), (
        f"unknown spec field must be rejected pointing at spec.extensions; got {errors!r}"
    )


def test_validate_raw_unknown_spec_field_does_not_collide_with_relocated_message():
    """Relocated fields get a specific redirect message, not the generic
    'unknown field' one. Keep the two messages distinct so operators
    fixing a v2 spec see the relocation hint instead of a vague typo
    complaint."""
    # Arrange — spec.model is a v3-RELOCATED field (→ spec.claude.model).
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer", "model": "opus"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert — the relocation hint is present AND the generic
    # "Unknown spec field" message is NOT also emitted for the same key.
    assert any("spec.claude.model" in e for e in errors) and not any(
        "Unknown spec field 'model'" in e for e in errors
    ), f"spec.model must produce only a relocation hint; got {errors!r}"


def test_validate_raw_rejects_metadata_name():
    """The v2-era ``metadata.name`` field is rejected with a redirect to
    the dir-as-SSoT layout."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"name": "legacy-agent"},
        "spec": {"runtime": "apptainer"},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert — rejected AND the message points at the dir-as-SSoT layout.
    assert any("metadata.name" in e and "parent directory" in e for e in errors), (
        f"metadata.name must be rejected pointing at dir-as-SSoT; got {errors!r}"
    )


def test_validate_raw_rejects_dot_claude():
    """The legacy ``spec.dot_claude`` layout key is rejected with a
    redirect to the ``to_home/`` deploy pipeline (ADR-0006)."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer", "dot_claude": {"skills": []}},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("dot_claude" in e and "to_home" in e for e in errors), (
        f"spec.dot_claude must be rejected pointing at the to_home pipeline; got {errors!r}"
    )


def test_validate_raw_rejects_spec_skills():
    """The v2-era ``spec.skills`` block is rejected; skills now live as
    files under ``to_home/.claude/skills/``."""
    # Arrange
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer", "skills": {"required": ["foo"]}},
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert any("spec.skills" in e and "to_home" in e for e in errors), (
        f"spec.skills must be rejected pointing at to_home/.claude/skills/; got {errors!r}"
    )


# ---------------------------------------------------------------------------
# spec.access + apptainer.container_workdir — REMOVED 2026-06-23 (SSoT:
# host access + cwd are declared explicitly via apptainer.binds + workdir).
# A spec carrying either is rejected LOUD with the exact replacement.
# ---------------------------------------------------------------------------


def _access_spec(access):
    return {**_BASE, "spec": {**_BASE["spec"], "access": access}}


def test_access_field_is_rejected():
    # Arrange — a spec still carrying the removed `access:` knob.
    raw = _access_spec("full")
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "spec.access has been REMOVED" in e]


def test_access_absent_passes():
    # Arrange — no `access` field (the only valid state now).
    raw = _BASE
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "spec.access" in e]


def test_access_rejection_points_at_explicit_binds():
    # Arrange — the rejection must hand the operator the replacement.
    raw = _access_spec("capsule")
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "spec.access has been REMOVED" in e]
    # Assert
    assert "apptainer.binds" in bad[0]


def test_container_workdir_is_rejected():
    # Arrange — the removed in-container workdir alias.
    raw = {
        **_BASE,
        "spec": {
            **_BASE["spec"],
            "apptainer": {
                **(_BASE["spec"].get("apptainer") or {}),
                "container_workdir": "/work",
            },
        },
    }
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "container_workdir has been REMOVED" in e]


# ---------------------------------------------------------------------------
# metadata.labels.tags — ABOLISHED (operator decision 2026-07-19).
#
# `groups:` is the only classification field. Every one of the 16 fleet
# specs that carried `tags: "active-development"` ALSO carried `active`
# inside `groups:`, so `tags` carried ZERO information `groups` did not
# already carry — pure duplication, i.e. the SSoT violation constitution
# §1 forbids. Rejected LOUDLY (no silent-accept transition window, per
# constitution §2 no-silent-fallbacks): a silently-ignored field is how
# dead fields survive for months.
# ---------------------------------------------------------------------------


def _labels_spec(labels: dict) -> dict:
    """A minimal valid v3 raw spec carrying ``metadata.labels``."""
    return {**_BASE, "metadata": {"labels": labels}}


def test_labels_tags_is_rejected():
    # Arrange — the abolished classification field.
    raw = _labels_spec({"tags": "active-development"})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert [e for e in errors if "metadata.labels.tags" in e]


def test_labels_tags_rejection_points_at_groups():
    # Arrange — the rejection must hand the operator the replacement field.
    raw = _labels_spec({"tags": "active-development"})
    # Act
    errors = validate_raw(raw, path="<test>")
    bad = [e for e in errors if "metadata.labels.tags" in e]
    # Assert
    assert "groups" in bad[0]


def test_labels_tags_rejection_names_the_offending_file():
    # Arrange — an operator fixing 16 specs needs to know WHICH one failed.
    raw = _labels_spec({"tags": "active-development"})
    # Act
    errors = validate_raw(raw, path="/agents/figrecipe/spec.yaml")
    bad = [e for e in errors if "metadata.labels.tags" in e]
    # Assert
    assert "/agents/figrecipe/spec.yaml" in bad[0]


def test_labels_groups_only_is_accepted():
    # Arrange — CONTROL: the surviving field must still validate cleanly,
    # so the rejection above cannot "pass" by rejecting every spec.
    raw = _labels_spec({"groups": ["developer", "active"]})
    # Act
    errors = validate_raw(raw, path="<test>")
    # Assert
    assert not [e for e in errors if "metadata.labels" in e]
