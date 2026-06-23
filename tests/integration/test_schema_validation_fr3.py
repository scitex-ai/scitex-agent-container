"""FR#3 — strict schema validation + explicit labels.description.

Tests:
  1. Unknown spec fields are rejected at parse time.
  2. Unknown top-level fields are rejected at parse time.
  3. labels.description is used as the explicit A2A card description.
  4. The implicit capabilities[0] → description fallback is removed.
  5. Existing shared agent YAMLs still validate clean.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007). Same-
shape invariants over one arrange/act collapse into
``pytest.parametrize``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(data: dict, name: str = "test-agent") -> Path:
    """Write YAML into dir-as-SSoT layout; strip metadata.name if present."""
    import copy

    data = copy.deepcopy(data)
    metadata = data.get("metadata") or {}
    metadata.pop("name", None)
    if metadata:
        data["metadata"] = metadata
    elif "metadata" in data:
        del data["metadata"]
    tmp = Path(tempfile.mkdtemp()) / name
    tmp.mkdir(parents=True)
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# No-hidden-defaults (operator directive 2026-06-23): a loadable spec must
# declare every applicable author field, so ``_BASE`` carries the full
# required set. Rejection tests add their bad field on top (still rejected);
# ``test_known_spec_fields_accepted_*`` loads this clean.
_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {
        "runtime": "apptainer",
        "host": "local",
        "workdir": "~/.scitex/agent-container/runtime/agents/test-agent",
        "claude": {"model": "claude-opus-4-8[1m]"},
        "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
    },
}


def _v3_with_labels(labels: dict) -> dict:
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": labels},
        "spec": {"runtime": "apptainer"},
    }


# ---------------------------------------------------------------------------
# 1 & 2 — unknown-field rejection
# ---------------------------------------------------------------------------


class TestUnknownFieldRejection:
    def test_unknown_spec_field_raises_value_error_naming_field(self):
        # Arrange
        from scitex_agent_container.config import load_config

        data = {
            **_BASE,
            "spec": {"runtime": "apptainer", "cardinality_enforced_at_hub": True},
        }
        path = _write_yaml(data)
        # Act
        action = lambda: load_config(path)
        # Assert
        with pytest.raises(ValueError, match="cardinality_enforced_at_hub"):
            action()

    def test_unknown_top_level_field_raises_value_error_naming_field(self):
        # Arrange
        from scitex_agent_container.config import load_config

        data = {**_BASE, "stale_field": "oops"}
        path = _write_yaml(data)
        # Act
        action = lambda: load_config(path)
        # Assert
        with pytest.raises(ValueError, match="stale_field"):
            action()

    def test_known_spec_fields_accepted_preserves_runtime(self):
        # Arrange
        from scitex_agent_container.config import load_config

        data = {
            **_BASE,
            "spec": {
                # Keep the required scaffold from _BASE, override with the
                # known fields under test (claude.model wins over the base).
                **_BASE["spec"],
                "runtime": "apptainer",
                "claude": {"model": "sonnet"},
                "a2a": {"port": 9999},
                "extensions": {"my_custom": "value"},
            },
        }
        path = _write_yaml(data)
        # Act
        cfg = load_config(path)
        # Assert
        assert cfg.runtime == "apptainer"

    @pytest.mark.parametrize("bad_field", ["bad_field", "another_bad"])
    def test_validate_raw_reports_each_unknown_spec_field_by_name(self, bad_field):
        # Arrange
        from scitex_agent_container.config._validation import validate_raw

        raw = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "spec": {"runtime": "apptainer", "bad_field": 1, "another_bad": 2},
        }
        # Act
        errors = validate_raw(raw, "test.yaml")
        # Assert
        assert bad_field in "\n".join(errors)


# ---------------------------------------------------------------------------
# 3 & 4 — labels.description → card.description
# ---------------------------------------------------------------------------


class TestLabelsDescription:
    def test_explicit_labels_description_used_as_card_description(self):
        # Arrange
        from scitex_agent_container.a2a._card import project_card

        v3 = _v3_with_labels({"description": "My explicit agent description"})
        # Act
        card = project_card("my-agent", v3, "http://localhost")
        # Assert
        assert card["description"] == "My explicit agent description"

    def test_role_label_fallback_used_as_card_description_when_no_description(self):
        # Arrange
        from scitex_agent_container.a2a._card import project_card

        v3 = _v3_with_labels({"role": "researcher"})
        # Act
        card = project_card("my-agent", v3, "http://localhost")
        # Assert
        assert card["description"] == "sac agent: my-agent (researcher)"

    def test_default_description_fallback_when_no_description_or_role(self):
        # Arrange
        from scitex_agent_container.a2a._card import project_card

        v3 = _v3_with_labels({})
        # Act
        card = project_card("my-agent", v3, "http://localhost")
        # Assert
        assert card["description"] == "sac agent: my-agent"

    def test_capabilities_label_does_not_become_card_description(self):
        """The implicit capabilities[0] → description fallback is removed."""
        # Arrange
        from scitex_agent_container.a2a._card import project_card

        v3 = _v3_with_labels({"capabilities": "search,index"})
        # Act
        card = project_card("my-agent", v3, "http://localhost")
        # Assert
        assert card["description"] == "sac agent: my-agent"


# ---------------------------------------------------------------------------
# 5 — existing shared agent YAMLs: valid ones still pass, invalid ones fail
# ---------------------------------------------------------------------------

_SHARED_AGENTS_DIR = Path.home() / ".scitex" / "orochi" / "shared" / "agents"

# Mirror of validator's _KNOWN_SPEC_KEYS — re-imported instead of
# duplicated so additions in _validation.py don't silently misclassify
# valid YAMLs as "invalid" here (which they did for image/dockerfile
# after F-CS16 phase 2a flattened spec.container.image).
from scitex_agent_container.config._validation import (
    _KNOWN_SPEC_KEYS as _KNOWN_SPEC_KEYS,
)


def _classify_v3_yamls():
    """Return (valid_paths, invalid_paths) for v3 YAMLs under shared/agents."""
    valid, invalid = [], []
    if not _SHARED_AGENTS_DIR.exists():
        return valid, invalid
    for yaml_path in sorted(_SHARED_AGENTS_DIR.rglob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_path.read_text())
        except (
            Exception
        ):  # stx-allow: fallback (reason: skip unreadable test fixture YAMLs)
            continue
        if not isinstance(raw, dict):
            continue
        if not raw.get("apiVersion", "").startswith("scitex-agent-container"):
            continue
        spec = raw.get("spec") or {}
        unknown = set(spec.keys()) - _KNOWN_SPEC_KEYS
        if unknown:
            invalid.append((yaml_path, sorted(unknown)))
        else:
            valid.append(yaml_path)
    return valid, invalid


_VALID_YAMLS, _INVALID_YAMLS = _classify_v3_yamls()

# Flatten (yaml_path, [field1, field2]) -> [(yaml_path, field1), ...] so
# each (path, single-field) row becomes one test with one assertion.
_INVALID_YAMLS_FLAT = [
    (path, field) for path, fields in _INVALID_YAMLS for field in fields
]


@pytest.mark.parametrize("yaml_path", _VALID_YAMLS, ids=lambda p: p.stem)
def test_valid_shared_agent_yaml_emits_no_unknown_field_errors(yaml_path):
    """Agents without unknown spec fields produce no FR#3 validation errors."""
    # Arrange
    from scitex_agent_container.config._validation import validate_raw

    raw = yaml.safe_load(yaml_path.read_text())
    # Act
    errors = validate_raw(raw, str(yaml_path))
    unknown_errors = [
        e for e in errors if "Unknown spec field" in e or "Unknown top-level field" in e
    ]
    # Assert
    assert not unknown_errors, f"Unexpected unknown-field errors: {unknown_errors}"


@pytest.mark.parametrize(
    "yaml_path,unknown_field",
    _INVALID_YAMLS_FLAT,
    ids=lambda x: x.stem if isinstance(x, Path) else str(x),
)
def test_invalid_shared_agent_yaml_validation_error_names_unknown_field(
    yaml_path, unknown_field
):
    """Agents with unknown spec fields now produce errors naming those fields."""
    # Arrange
    from scitex_agent_container.config._validation import validate_raw

    raw = yaml.safe_load(yaml_path.read_text())
    # Act
    errors = validate_raw(raw, str(yaml_path))
    # Assert
    assert unknown_field in "\n".join(errors)
