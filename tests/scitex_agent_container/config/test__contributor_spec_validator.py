"""Pytest for contributor spec YAML schema validator (ZOO#01 chunk C).

Validates that validate_contributor_spec_raw() correctly accepts/rejects
rendered contributor spec.yaml payloads for all required fields:
  apiVersion, kind, metadata.labels, spec.runtime, spec.host,
  spec.a2a.port, spec.startup_commands.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import (
    validate_contributor_spec,
    validate_contributor_spec_raw,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_CONTRIBUTOR_SPEC: dict = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {
        "labels": {
            "role": "contributor-scitex-agent-container",
            "team": "orochi",
            "trigger": "pr-driven",
            "project": "scitex-agent-container",
            "branch_kind": "feat",
            "branch_short": "spec-template-validate",
        }
    },
    "spec": {
        "runtime": "apptainer",
        "host": ["spartan", "spartan-bm149"],
        "a2a": {
            "port": 19132,
            "handler": "claude_cli",
            "host": "127.0.0.1",
        },
        "startup_commands": [
            {
                "delay": 5,
                "command": "Boot per src_CLAUDE.md. Mission: validate contributor spec.",
            }
        ],
    },
}


def _patch(base: dict, *path_and_value) -> dict:
    """Return a deep copy of base with path set to value, or key deleted.

    Call as _patch(base, "spec", "a2a", "port", 99) to set nested key.
    Pass sentinel DELETE as value to remove the key.
    """
    import copy

    DELETE = object()
    if len(path_and_value) < 2:
        raise ValueError("need at least one key and a value")
    keys = path_and_value[:-1]
    value = path_and_value[-1]
    d = copy.deepcopy(base)
    node = d
    for k in keys[:-1]:
        node = node[k]
    if value is DELETE:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value
    return d, DELETE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _errors(raw: dict) -> list[str]:
    return validate_contributor_spec_raw(raw, "<test>")


def _write_and_validate(data: dict) -> list[str]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(data, f)
        path = f.name
    result = validate_contributor_spec(path)
    Path(path).unlink()
    return result


# ---------------------------------------------------------------------------
# Patch helper (simpler version without sentinel)
# ---------------------------------------------------------------------------


def _set(base: dict, keys: list, value) -> dict:
    import copy

    d = copy.deepcopy(base)
    node = d
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return d


def _del(base: dict, keys: list) -> dict:
    import copy

    d = copy.deepcopy(base)
    node = d
    for k in keys[:-1]:
        node = node[k]
    node.pop(keys[-1], None)
    return d


# ---------------------------------------------------------------------------
# Valid spec
# ---------------------------------------------------------------------------


class TestValidSpec:
    def test_valid_contributor_spec_has_no_errors(self):
        assert _errors(VALID_CONTRIBUTOR_SPEC) == []

    def test_valid_via_file(self):
        assert _write_and_validate(VALID_CONTRIBUTOR_SPEC) == []


# ---------------------------------------------------------------------------
# apiVersion
# ---------------------------------------------------------------------------


class TestApiVersion:
    def test_missing_api_version(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["apiVersion"])
        errors = _errors(raw)
        assert any("apiVersion" in e for e in errors)

    def test_wrong_api_version(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["apiVersion"], "scitex-agent-container/v2")
        errors = _errors(raw)
        assert any("apiVersion" in e for e in errors)

    def test_correct_api_version(self):
        errors = _errors(VALID_CONTRIBUTOR_SPEC)
        assert not any("apiVersion" in e for e in errors)


# ---------------------------------------------------------------------------
# kind
# ---------------------------------------------------------------------------


class TestKind:
    def test_missing_kind(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["kind"])
        errors = _errors(raw)
        assert any("kind" in e for e in errors)

    def test_wrong_kind(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["kind"], "Service")
        errors = _errors(raw)
        assert any("kind" in e for e in errors)

    def test_correct_kind(self):
        errors = _errors(VALID_CONTRIBUTOR_SPEC)
        assert not any("kind" in e for e in errors)


# ---------------------------------------------------------------------------
# metadata.labels
# ---------------------------------------------------------------------------


class TestMetadataLabels:
    def test_missing_metadata(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["metadata"])
        errors = _errors(raw)
        assert any("metadata" in e for e in errors)

    def test_missing_labels(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["metadata", "labels"])
        errors = _errors(raw)
        assert any("metadata.labels" in e for e in errors)

    def test_labels_not_a_dict(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["metadata", "labels"], "string-value")
        errors = _errors(raw)
        assert any("metadata.labels" in e for e in errors)

    @pytest.mark.parametrize("key", ["role", "team", "trigger", "project"])
    def test_missing_required_label_key(self, key):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["metadata", "labels", key])
        errors = _errors(raw)
        assert any(f"metadata.labels.{key}" in e for e in errors)

    @pytest.mark.parametrize("key", ["role", "team", "trigger", "project"])
    def test_empty_required_label_key(self, key):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["metadata", "labels", key], "")
        errors = _errors(raw)
        assert any(f"metadata.labels.{key}" in e for e in errors)

    def test_extra_label_keys_allowed(self):
        import copy

        raw = copy.deepcopy(VALID_CONTRIBUTOR_SPEC)
        raw["metadata"]["labels"]["branch_kind"] = "feat"
        raw["metadata"]["labels"]["capabilities"] = "fork,clone"
        assert _errors(raw) == []


# ---------------------------------------------------------------------------
# spec.runtime
# ---------------------------------------------------------------------------


class TestSpecRuntime:
    def test_missing_spec_runtime_allowed(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["spec", "runtime"])
        errors = _errors(raw)
        assert not any("runtime" in e for e in errors)

    def test_invalid_runtime(self):
        # F-CS16 phase 2a: ``docker`` / ``podman`` / ``apptainer`` are
        # now valid runtime values (container engines). Use a clearly
        # bogus value to assert the validator still rejects unknowns.
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "runtime"], "no-such-engine")
        errors = _errors(raw)
        assert any("runtime" in e for e in errors)

    def test_valid_runtime_apptainer(self):
        """Apptainer is the only accepted runtime since the 2026-05-13
        docker/podman ripout."""
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "runtime"], "apptainer")
        errors = _errors(raw)
        assert not any("runtime" in e for e in errors)

    @pytest.mark.parametrize("runtime", ["docker", "podman"])
    def test_docker_and_podman_rejected(self, runtime):
        """docker / podman were valid runtimes pre-2026-05-13; the
        ripout makes them errors."""
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "runtime"], runtime)
        errors = _errors(raw)
        assert any("runtime" in e for e in errors)


# ---------------------------------------------------------------------------
# spec.host
# ---------------------------------------------------------------------------


class TestSpecHost:
    def test_host_as_list(self):
        raw = _set(
            VALID_CONTRIBUTOR_SPEC, ["spec", "host"], ["spartan", "spartan-bm149"]
        )
        assert _errors(raw) == []

    def test_host_as_string(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "host"], "spartan")
        assert _errors(raw) == []

    def test_host_and_hosts_mutually_exclusive(self):
        import copy

        raw = copy.deepcopy(VALID_CONTRIBUTOR_SPEC)
        raw["spec"]["hosts"] = ["spartan"]
        errors = _errors(raw)
        assert any("mutually exclusive" in e for e in errors)

    def test_host_list_must_contain_strings(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "host"], [1, 2])
        errors = _errors(raw)
        assert any("host" in e for e in errors)


# ---------------------------------------------------------------------------
# spec.a2a.port
# ---------------------------------------------------------------------------


class TestSpecA2APort:
    def test_missing_a2a(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["spec", "a2a"])
        errors = _errors(raw)
        assert any("spec.a2a" in e for e in errors)

    def test_a2a_not_a_dict(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "a2a"], 19132)
        errors = _errors(raw)
        assert any("spec.a2a" in e for e in errors)

    def test_missing_port(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["spec", "a2a", "port"])
        errors = _errors(raw)
        assert any("spec.a2a.port" in e for e in errors)

    def test_port_not_integer(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "a2a", "port"], "19132")
        errors = _errors(raw)
        assert any("spec.a2a.port" in e for e in errors)

    def test_port_below_minimum(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "a2a", "port"], 80)
        errors = _errors(raw)
        assert any("spec.a2a.port" in e for e in errors)

    def test_port_above_maximum(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "a2a", "port"], 99999)
        errors = _errors(raw)
        assert any("spec.a2a.port" in e for e in errors)

    @pytest.mark.parametrize("port", [1024, 8080, 19132, 65535])
    def test_valid_ports(self, port):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "a2a", "port"], port)
        errors = _errors(raw)
        assert not any("spec.a2a.port" in e for e in errors)


# ---------------------------------------------------------------------------
# spec.startup_commands
# ---------------------------------------------------------------------------


class TestSpecStartupCommands:
    def test_missing_startup_commands(self):
        raw = _del(VALID_CONTRIBUTOR_SPEC, ["spec", "startup_commands"])
        errors = _errors(raw)
        assert any("startup_commands" in e for e in errors)

    def test_startup_commands_not_a_list(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "startup_commands"], "run me")
        errors = _errors(raw)
        assert any("startup_commands" in e for e in errors)

    def test_startup_commands_empty_list(self):
        raw = _set(VALID_CONTRIBUTOR_SPEC, ["spec", "startup_commands"], [])
        errors = _errors(raw)
        assert any("startup_commands" in e for e in errors)

    def test_startup_command_missing_command_field(self):
        raw = _set(
            VALID_CONTRIBUTOR_SPEC,
            ["spec", "startup_commands"],
            [{"delay": 5}],
        )
        errors = _errors(raw)
        assert any("startup_commands[0].command" in e for e in errors)

    def test_startup_command_empty_command_field(self):
        raw = _set(
            VALID_CONTRIBUTOR_SPEC,
            ["spec", "startup_commands"],
            [{"delay": 5, "command": ""}],
        )
        errors = _errors(raw)
        assert any("startup_commands[0].command" in e for e in errors)

    def test_startup_commands_not_a_mapping(self):
        raw = _set(
            VALID_CONTRIBUTOR_SPEC,
            ["spec", "startup_commands"],
            ["bare string command"],
        )
        errors = _errors(raw)
        assert any("startup_commands[0]" in e for e in errors)

    def test_valid_startup_commands_multiple(self):
        raw = _set(
            VALID_CONTRIBUTOR_SPEC,
            ["spec", "startup_commands"],
            [
                {"delay": 0, "command": "echo hello"},
                {"delay": 5, "command": "Boot per spec."},
            ],
        )
        assert _errors(raw) == []

    def test_valid_startup_commands_no_delay(self):
        raw = _set(
            VALID_CONTRIBUTOR_SPEC,
            ["spec", "startup_commands"],
            [{"command": "Boot per spec."}],
        )
        assert _errors(raw) == []


# ---------------------------------------------------------------------------
# validate_contributor_spec (file-based)
# ---------------------------------------------------------------------------


class TestValidateContributorSpecFile:
    def test_file_not_found(self):
        errors = validate_contributor_spec("/nonexistent/path.yaml")
        assert any("not found" in e.lower() for e in errors)

    def test_invalid_yaml_syntax(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("key: [\nunot closed")
            path = f.name
        errors = validate_contributor_spec(path)
        Path(path).unlink()
        assert any("YAML" in e or "parse" in e.lower() for e in errors)

    def test_real_agent_spec_is_valid(self):
        spec_path = (
            Path.home()
            / ".scitex/agent-container/agents/c-sac-spec-template-validate/c-sac-spec-template-validate.yaml"
        )
        if not spec_path.exists():
            pytest.skip(f"Agent spec not found: {spec_path}")
        errors = validate_contributor_spec(spec_path)
        assert errors == [], "Real agent spec failed validation:\n" + "\n".join(errors)
