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
        "runtime": "claude-session",
    },
}


def _spec(model):
    return {**_BASE, "spec": {**_BASE["spec"], "model": model}}


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
    bad = [e for e in errors if "spec.model" in e]
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
    bad = [e for e in errors if "spec.model" in e]
    assert bad, f"expected spec.model rejection for {model!r}, got none"
    msg = bad[0]
    assert model in msg, "error must echo the offending model string"
    assert "claude-opus-4-7" in msg or "alias" in msg.lower(), (
        "error must point the user at the canonical forms"
    )


def test_missing_model_is_allowed():
    """Empty / missing model is fine — runtime falls back to its default."""
    errors = validate_raw(_BASE, path="<test>")
    assert not [e for e in errors if "spec.model" in e]


def test_non_string_model_rejected():
    """Numbers, lists, nulls etc. must be rejected with a typed error."""
    errors = validate_raw(_spec(42), path="<test>")
    bad = [e for e in errors if "spec.model" in e]
    assert bad
    assert "string" in bad[0].lower()


# ---------------------------------------------------------------------------
# F-CS6 — yaml-field rename: spec.runtime soft alias
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_runtime_warning_marker(tmp_path, monkeypatch):
    """Each test gets its own XDG_RUNTIME_DIR so the once-per-shell
    marker file doesn't leak warnings between cases."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    yield


def test_normalize_runtime_passthrough_for_canonical():
    from scitex_agent_container.config._validation import normalize_runtime

    assert normalize_runtime("claude-code") == "claude-code"
    assert normalize_runtime("claude-session") == "claude-session"
    assert normalize_runtime("slurm") == "slurm"
    assert normalize_runtime("slurm-tenant") == "slurm-tenant"


def test_normalize_runtime_returns_none_for_none():
    from scitex_agent_container.config._validation import normalize_runtime

    assert normalize_runtime(None) is None


def test_normalize_runtime_maps_aliases_to_canonical():
    from scitex_agent_container.config._validation import normalize_runtime

    assert normalize_runtime("claude-cli-tui") == "claude-code"
    assert normalize_runtime("claude-sdk-persistent") == "claude-session"


def test_normalize_runtime_warns_once_per_shell(capsys):
    from scitex_agent_container.config._validation import normalize_runtime

    normalize_runtime("claude-cli-tui")
    first = capsys.readouterr().err
    assert "claude-cli-tui" in first
    assert "F-CS6" in first

    normalize_runtime("claude-cli-tui")
    second = capsys.readouterr().err
    assert second == "", "warning must fire only once per shell-session marker"


def test_normalize_runtime_warns_per_distinct_alias(capsys):
    """Two different aliases each get their own marker -> each warns once."""
    from scitex_agent_container.config._validation import normalize_runtime

    normalize_runtime("claude-cli-tui")
    normalize_runtime("claude-sdk-persistent")
    err = capsys.readouterr().err
    assert "claude-cli-tui" in err
    assert "claude-sdk-persistent" in err


def test_validate_raw_accepts_alias_runtime():
    """spec.runtime: claude-cli-tui (or claude-sdk-persistent) must
    pass validation in addition to the canonical names."""
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": "claude-sdk-persistent"},
    }
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.runtime" in e]


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
        "spec": {"runtime": "claude-session", "autonomous": autonomous},
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
        "spec": {"runtime": "claude-session"},
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
        "spec": {"runtime": "docker", **extra},
    }


@pytest.mark.parametrize("engine", ["docker", "podman", "apptainer"])
def test_validate_raw_accepts_new_engine_runtime(engine):
    """The new ``runtime`` field carries the container engine name.
    docker / podman / apptainer must all pass validation."""
    raw = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"runtime": engine},
    }
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.runtime" in e]


def test_validate_raw_still_accepts_legacy_runtime_for_one_cycle():
    """Legacy values must keep parsing through F-CS16 phase 2e."""
    for legacy in (
        "claude-code",
        "claude-cli-tui",
        "claude-session",
        "claude-sdk-persistent",
        "slurm",
        "slurm-tenant",
    ):
        raw = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "spec": {"runtime": legacy},
        }
        errors = validate_raw(raw, path="<test>")
        assert not [e for e in errors if "spec.runtime" in e], (
            f"legacy runtime {legacy!r} must keep parsing during the migration"
        )


def test_validate_raw_accepts_top_level_image_field():
    raw = _spec_with({"image": "scitex-agent-container:sdk-persistent"})
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.image" in e]


def test_validate_raw_rejects_non_string_image():
    raw = _spec_with({"image": 42})
    errors = validate_raw(raw, path="<test>")
    assert any("spec.image" in e and "string" in e for e in errors)


def test_validate_raw_accepts_top_level_dockerfile_field():
    raw = _spec_with({"dockerfile": "./containers/Dockerfile.sdk-persistent"})
    errors = validate_raw(raw, path="<test>")
    assert not [e for e in errors if "spec.dockerfile" in e]


def test_validate_raw_rejects_non_string_dockerfile():
    raw = _spec_with({"dockerfile": ["./a", "./b"]})
    errors = validate_raw(raw, path="<test>")
    assert any("spec.dockerfile" in e and "string" in e for e in errors)


def test_image_and_dockerfile_default_to_empty_in_agentconfig(tmp_path):
    """A yaml without ``image`` / ``dockerfile`` must still parse;
    AgentConfig fields stay empty so phase 2d's auto-build path can
    apply its defaults at dispatch time."""
    import yaml as _yaml

    from scitex_agent_container.config import load_config

    yaml_dir = tmp_path / "fcs16-default"
    yaml_dir.mkdir()
    yaml_path = yaml_dir / "fcs16-default.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": {"runtime": "docker"},
            }
        )
    )
    cfg = load_config(str(yaml_path))
    assert cfg.image == ""
    assert cfg.dockerfile == ""


def test_image_and_dockerfile_round_trip_through_loader(tmp_path):
    import yaml as _yaml

    from scitex_agent_container.config import load_config

    yaml_dir = tmp_path / "fcs16-set"
    yaml_dir.mkdir()
    yaml_path = yaml_dir / "fcs16-set.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": {
                    "runtime": "docker",
                    "image": "clew-paper:capsule-01",
                    "dockerfile": "./containers/clew-paper.Dockerfile",
                },
            }
        )
    )
    cfg = load_config(str(yaml_path))
    assert cfg.image == "clew-paper:capsule-01"
    assert cfg.dockerfile == "./containers/clew-paper.Dockerfile"
