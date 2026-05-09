"""FR#3 — strict schema validation + explicit labels.description.

Tests:
  1. Unknown spec fields are rejected at parse time.
  2. Unknown top-level fields are rejected at parse time.
  3. labels.description is used as the explicit A2A card description.
  4. The implicit capabilities[0] → description fallback is removed.
  5. Existing shared agent YAMLs still validate clean.
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


_BASE = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "spec": {"runtime": "docker"},
}


# ---------------------------------------------------------------------------
# 1 & 2 — unknown-field rejection
# ---------------------------------------------------------------------------


class TestUnknownFieldRejection:
    def test_unknown_spec_field_rejected(self):
        from scitex_agent_container.config import load_config

        data = {
            **_BASE,
            "spec": {"runtime": "docker", "cardinality_enforced_at_hub": True},
        }
        path = _write_yaml(data)
        with pytest.raises(ValueError, match="cardinality_enforced_at_hub"):
            load_config(path)

    def test_unknown_top_level_field_rejected(self):
        from scitex_agent_container.config import load_config

        data = {**_BASE, "stale_field": "oops"}
        path = _write_yaml(data)
        with pytest.raises(ValueError, match="stale_field"):
            load_config(path)

    def test_known_spec_fields_accepted(self):
        from scitex_agent_container.config import load_config

        data = {
            **_BASE,
            "spec": {
                "runtime": "docker",
                "model": "sonnet",
                "a2a": {"port": 9999},
                "extensions": {"my_custom": "value"},
            },
        }
        path = _write_yaml(data)
        cfg = load_config(path)
        assert cfg.runtime == "docker"

    def test_validate_raw_returns_errors_for_unknown_spec(self):
        from scitex_agent_container.config._validation import validate_raw

        raw = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "spec": {"runtime": "docker", "bad_field": 1, "another_bad": 2},
        }
        errors = validate_raw(raw, "test.yaml")
        messages = "\n".join(errors)
        assert "bad_field" in messages
        assert "another_bad" in messages


# ---------------------------------------------------------------------------
# 3 & 4 — labels.description → card.description
# ---------------------------------------------------------------------------


class TestLabelsDescription:
    def _v3(self, labels: dict) -> dict:
        return {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"labels": labels},
            "spec": {"runtime": "docker"},
        }

    def test_explicit_description_used(self):
        from scitex_agent_container.a2a._card import project_card

        v3 = self._v3({"description": "My explicit agent description"})
        card = project_card("my-agent", v3, "http://localhost")
        assert card["description"] == "My explicit agent description"

    def test_role_fallback_when_no_description(self):
        from scitex_agent_container.a2a._card import project_card

        v3 = self._v3({"role": "researcher"})
        card = project_card("my-agent", v3, "http://localhost")
        assert card["description"] == "sac agent: my-agent (researcher)"

    def test_default_fallback_when_no_description_or_role(self):
        from scitex_agent_container.a2a._card import project_card

        v3 = self._v3({})
        card = project_card("my-agent", v3, "http://localhost")
        assert card["description"] == "sac agent: my-agent"

    def test_capabilities_no_longer_used_as_description(self):
        """The implicit capabilities[0] → description fallback is removed."""
        from scitex_agent_container.a2a._card import project_card

        v3 = self._v3({"capabilities": "search,index"})
        card = project_card("my-agent", v3, "http://localhost")
        # capabilities should NOT appear as the card description
        assert card["description"] != "Search"
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
        except Exception:
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


@pytest.mark.parametrize("yaml_path", _VALID_YAMLS, ids=lambda p: p.stem)
def test_valid_shared_agent_yaml_no_unknown_spec_fields(yaml_path):
    """Agents without unknown spec fields produce no FR#3 validation errors."""
    from scitex_agent_container.config._validation import validate_raw

    raw = yaml.safe_load(yaml_path.read_text())
    errors = validate_raw(raw, str(yaml_path))
    unknown_errors = [
        e for e in errors if "Unknown spec field" in e or "Unknown top-level field" in e
    ]
    assert not unknown_errors, f"Unexpected unknown-field errors: {unknown_errors}"


@pytest.mark.parametrize(
    "yaml_path,unknown_fields",
    _INVALID_YAMLS,
    ids=lambda x: x.stem if isinstance(x, Path) else str(x),
)
def test_invalid_shared_agent_yaml_now_rejected(yaml_path, unknown_fields):
    """Agents with unknown spec fields now produce errors naming those fields."""
    from scitex_agent_container.config._validation import validate_raw

    raw = yaml.safe_load(yaml_path.read_text())
    errors = validate_raw(raw, str(yaml_path))
    error_msg = "\n".join(errors)
    for field in unknown_fields:
        assert field in error_msg, f"Expected '{field}' in validation errors"
