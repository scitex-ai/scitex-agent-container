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
