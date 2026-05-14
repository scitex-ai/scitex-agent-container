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
    errors = validate_raw(_spec(model), path="<test>")
    bad = [e for e in errors if "spec.claude.model" in e]
    assert bad == [], f"unexpected spec.model errors for {model!r}: {bad}"


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus[1m]",  # the F-CS7 reproducer — abbreviated, no version
        "claude-opus",
        "claude-sonnet",
        "claude-haiku",
        "opusx",
        "claude-foo-1-2",  # unknown family
    ],
)
def test_invalid_models_rejected(model):
    """Abbreviated / unknown forms must fail validation with a redirect."""
    errors = validate_raw(_spec(model), path="<test>")
    bad = [e for e in errors if "spec.claude.model" in e]
    assert bad, f"expected spec.model rejection for {model!r}, got none"
    msg = bad[0]
    assert model in msg, "error must echo the offending model string"
    assert "claude-opus-4-7" in msg or "alias" in msg.lower(), (
        "error must point the user at the canonical forms"
    )


def test_missing_model_is_allowed():
    """Empty / missing model is fine — runtime falls back to its default."""
    errors = validate_raw(_BASE, path="<test>")
    assert not [e for e in errors if "spec.claude.model" in e]


def test_non_string_model_rejected():
    """Numbers, lists, nulls etc. must be rejected with a typed error."""
    errors = validate_raw(_spec(42), path="<test>")
    bad = [e for e in errors if "spec.claude.model" in e]
    assert bad
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


def test_validate_raw_rejects_f_cs6_aliases_after_f_cs17():
    """F-CS17 stage 2 rejects every legacy / aliased runtime value.
    F-CS6's yaml-friendly aliases (claude-cli-tui /
    claude-sdk-persistent) hard-error alongside their canonical
    forms — sac is container-only."""
    for alias in ("claude-cli-tui", "claude-sdk-persistent"):
        raw = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "spec": {"runtime": alias},
        }
        errors = validate_raw(raw, path="<test>")
        assert any("spec.runtime" in e and alias in e for e in errors), (
            f"alias {alias!r} must hard-error with a redirect"
        )


def test_validate_raw_rejects_unknown_runtime():
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "claude-xtreme"},
    }
    errors = validate_raw(raw, path="<test>")
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
    raw = _autonomous_spec(
        {
            "enabled": True,
            "drive_until": "DONE",
            "max_turns": 50,
            "idle_kick_after_s": 120,
            "kick_text": "Continue. Print DONE when finished.",
        }
    )
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.autonomous" in e]


def test_autonomous_block_optional():
    """No autonomous block at all is fine — defaults apply at parse time."""
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer"},
    }
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.autonomous" in e]


def test_autonomous_must_be_mapping():
    raw = _autonomous_spec("not-a-dict")
    errors = validate_raw(raw, path="<test>")
    assert any("spec.autonomous must be a mapping" in e for e in errors)


def test_autonomous_drive_until_must_be_nonempty_string():
    errors = validate_raw(_autonomous_spec({"drive_until": ""}), path="<test>")
    assert any("drive_until" in e and "non-empty" in e for e in errors)
    errors = validate_raw(_autonomous_spec({"drive_until": 42}), path="<test>")
    assert any("drive_until must be a string" in e for e in errors)


def test_autonomous_max_turns_must_be_positive_int():
    errors = validate_raw(_autonomous_spec({"max_turns": 0}), path="<test>")
    assert any("max_turns must be > 0" in e for e in errors)
    errors = validate_raw(_autonomous_spec({"max_turns": "fifty"}), path="<test>")
    assert any("max_turns must be an integer" in e for e in errors)
    errors = validate_raw(_autonomous_spec({"max_turns": True}), path="<test>")
    # bool is a subclass of int — still rejected.
    assert any("max_turns must be an integer" in e for e in errors)


def test_autonomous_idle_kick_must_be_positive_int():
    errors = validate_raw(_autonomous_spec({"idle_kick_after_s": -1}), path="<test>")
    assert any("idle_kick_after_s must be > 0" in e for e in errors)


def test_autonomous_enabled_must_be_bool():
    errors = validate_raw(_autonomous_spec({"enabled": "yes"}), path="<test>")
    assert any("enabled must be a boolean" in e for e in errors)


def test_autonomous_parse_returns_defaults_when_missing():
    from scitex_agent_container.config._parsers import parse_autonomous
    from scitex_agent_container.config._types import AutonomousSpec

    out = parse_autonomous({})
    assert out == AutonomousSpec()


def test_autonomous_parse_reads_full_block():
    from scitex_agent_container.config._parsers import parse_autonomous

    out = parse_autonomous(
        {
            "autonomous": {
                "enabled": True,
                "drive_until": "ALL DONE",
                "max_turns": 7,
                "idle_kick_after_s": 60,
                "kick_text": "keep going",
            }
        }
    )
    assert out.enabled is True
    assert out.drive_until == "ALL DONE"
    assert out.max_turns == 7
    assert out.idle_kick_after_s == 60
    assert out.kick_text == "keep going"


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
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "apptainer"},
    }
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.runtime" in e]


@pytest.mark.parametrize(
    "legacy",
    [
        "docker",
        "podman",
        "claude-code",
        "claude-cli-tui",
        "claude-session",
        "claude-sdk-persistent",
        "slurm",
        "slurm-tenant",
    ],
)
def test_validate_raw_rejects_legacy_runtime_values(legacy):
    """Every non-apptainer runtime is rejected at parse time
    (docker / podman dropped 2026-05-13; legacy SDK strings even
    earlier)."""
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": legacy},
    }
    errors = validate_raw(raw, path="<test>")
    runtime_errors = [e for e in errors if "spec.runtime" in e]
    assert runtime_errors, f"legacy runtime {legacy!r} must be rejected"
    assert legacy in runtime_errors[0]


def test_validate_raw_accepts_apptainer_image_field():
    # v3-realign: image moved into spec.apptainer.image.
    raw = _spec_with({"apptainer": {"image": "scitex-agent-container:scitex"}})
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.apptainer.image" in e]


def test_validate_raw_rejects_top_level_image_field():
    """v3-realign: top-level spec.image is rejected with a relocation hint."""
    raw = _spec_with({"image": "scitex-agent-container:scitex"})
    errors = validate_raw(raw, path="<test>")
    assert any("spec.image" in e and "spec.apptainer.image" in e for e in errors)


def test_validate_raw_rejects_non_string_image():
    raw = _spec_with({"apptainer": {"image": 42}})
    errors = validate_raw(raw, path="<test>")
    assert any("spec.apptainer.image" in e and "string" in e for e in errors)


def test_validate_raw_rejects_non_string_dockerfile():
    """``spec.dockerfile`` is no longer interpreted (docker ripout
    2026-05-13), but a non-string value still surfaces a type error
    so an operator can't put a list there by accident."""
    raw = _spec_with({"dockerfile": ["./a", "./b"]})
    errors = validate_raw(raw, path="<test>")
    assert any("spec.dockerfile" in e and "string" in e for e in errors)


def test_image_defaults_to_empty_in_agentconfig(tmp_path):
    """A yaml without ``image`` must still parse; AgentConfig.image
    stays empty so the apptainer dispatcher applies its default
    (the sac-scitex SIF) at dispatch."""
    import yaml as _yaml

    from scitex_agent_container.config import load_config

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
    cfg = load_config(str(yaml_path))
    assert cfg.image == ""


def test_image_round_trip_through_loader(tmp_path):
    import yaml as _yaml

    from scitex_agent_container.config import load_config

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
    cfg = load_config(str(yaml_path))
    # v3: AgentConfig.image is populated from spec.apptainer.image.
    assert cfg.apptainer.image == (
        "~/.scitex/agent-container/containers/sac-scitex.sif"
    )
    assert cfg.image == "~/.scitex/agent-container/containers/sac-scitex.sif"
