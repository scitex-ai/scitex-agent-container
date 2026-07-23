"""Specification tests for the agreed v3 spec structure.

Source of truth: ``GITIGNORED/REQUIREMENT_SUMMARY.md §3``. These tests
encode the *target* shape — what `spec.yaml` should look like after
the in-progress v3 realignment lands. They double as:

* a green-when-fixed regression net for the realignment commits, and
* live documentation of the agreed boundaries (xfail tests surface
  the gap rather than hiding it behind a "TODO" comment).

Each test maps to one row of the README YAML Spec Reference table.
Tests that already pass document fields that landed correctly; tests
marked ``xfail(strict=True)`` document the gap items from
``REQUIREMENT_SUMMARY.md §3 -> Removed from v3``. When the underlying
field lands, the ``xfail`` will flip to XPASS and the suite will fail
with a clear "remove the xfail marker" message — that's the realign
checklist's done bell.

TQ cleanup (v3_spec_structure slice): every test carries AAA markers
and exactly one assertion. Multi-field round-trips (health, restart,
metadata.labels) collapse into ``pytest.parametrize`` over the
``(field-path, expected-value)`` tuples driving each assertion. Names
keep >= 3 word-tokens after ``test_`` to satisfy TQ003.
"""

from __future__ import annotations

from operator import attrgetter
from pathlib import Path

import pytest
import yaml as _yaml

from scitex_agent_container.config import AgentConfig, load_config, validate_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Explicit-fields red-start ruling (2026-07-21, superseding the 2026-06-23
# subset): EVERY spec field is REQUIRED at the YAML/load layer. ``_write_spec``
# fills the full scaffolding from the validator's own paste defaults so each
# test body only has to declare the field it exercises; the body still wins on
# any key (deep-merged), so round-trip and removed-field assertions are
# preserved.
# NOTE: ``runtime`` is intentionally NOT scaffolded — the body must supply it
# (every test does), keeping the validator's runtime-required rule honest.


def _required_scaffold() -> dict:
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_spec_defaults,
    )

    scaffold = explicit_spec_defaults("Agent")
    del scaffold["runtime"]
    scaffold["host"] = "${HOSTNAME}"
    scaffold["workdir"] = "~/.scitex/agent-container/runtime/agents/v3spec"
    scaffold["claude"]["model"] = "claude-opus-4-8[1m]"
    scaffold["apptainer"]["image"] = "/opt/sac/scitex.sif"
    return scaffold


def _merge_required(spec_body: dict) -> dict:
    """Deep-merge the required scaffold UNDER ``spec_body`` (body wins)."""
    from tests.scitex_agent_container._helpers.explicit_spec import deep_merge

    return deep_merge(_required_scaffold(), spec_body)


def _write_spec(tmp_path: Path, spec_body: dict, name: str = "v3spec") -> Path:
    """Write a dir-as-SSoT v3 spec.yaml under tmp_path; return its path."""
    agent_dir = tmp_path / name
    agent_dir.mkdir()
    spec_path = agent_dir / "spec.yaml"
    spec_path.write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": _merge_required(spec_body),
            }
        )
    )
    return spec_path


# ---------------------------------------------------------------------------
# Top-level cross-cutting fields — implemented today
# ---------------------------------------------------------------------------


class TestCrossCuttingTopLevel:
    """Fields that stay at the top level per §3 (workdir, runtime,
    a2a, health, restart, metadata.labels)."""

    def test_runtime_apptainer_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(tmp_path, {"runtime": "apptainer"})
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.runtime == "apptainer"

    def test_runtime_defaults_to_tui_when_omitted(self):
        # Arrange — runtime is now REQUIRED in YAML (no hidden default), so the
        # omitted-runtime default is only reachable by constructing the config
        # object directly (bypassing YAML validation). TUI is the default
        # launch mode (operator directive 2026-06-15).
        config = AgentConfig(name="agent-x")
        # Act
        runtime = config.runtime
        # Assert
        assert runtime == "tui"

    def test_workdir_value_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path, {"runtime": "apptainer", "workdir": "/tmp/agent-x"}
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.workdir == "/tmp/agent-x"

    def test_a2a_port_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(tmp_path, {"runtime": "apptainer", "a2a": {"port": 7901}})
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.a2a.port == 7901

    @pytest.mark.parametrize(
        "attr_path,expected",
        [
            ("health.enabled", True),
            ("health.interval", 60),
            ("health.method", "sdk-alive"),
        ],
    )
    def test_health_block_field_round_trips(self, tmp_path, attr_path, expected):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "health": {
                    "enabled": True,
                    "interval": 60,
                    "method": "sdk-alive",
                },
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert attrgetter(attr_path)(cfg) == expected

    @pytest.mark.parametrize(
        "attr_path,expected",
        [
            ("restart.policy", "on-failure"),
            ("restart.max_retries", 3),
            ("restart.backoff_initial", 30),
        ],
    )
    def test_restart_block_field_round_trips(self, tmp_path, attr_path, expected):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "restart": {
                    "policy": "on-failure",
                    "max_retries": 3,
                    "backoff_initial": 30,
                },
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert attrgetter(attr_path)(cfg) == expected

    @pytest.mark.parametrize(
        "label_key,label_value",
        [
            ("role", "researcher"),
            ("team", "lab-a"),
        ],
    )
    def test_metadata_labels_round_trip(self, tmp_path, label_key, label_value):
        # Arrange
        agent_dir = tmp_path / "labelled"
        agent_dir.mkdir()
        spec_path = agent_dir / "spec.yaml"
        spec_path.write_text(
            _yaml.safe_dump(
                {
                    "apiVersion": "scitex-agent-container/v3",
                    "kind": "Agent",
                    "metadata": {"labels": {"role": "researcher", "team": "lab-a"}},
                    "spec": _merge_required({"runtime": "apptainer"}),
                }
            )
        )
        # Act
        cfg = load_config(str(spec_path))
        # Assert
        assert cfg.labels.get(label_key) == label_value


class TestDirAsSSoT:
    """§3 — there's no ``metadata.name`` field; the agent name is the
    parent directory of spec.yaml."""

    def test_name_derived_from_parent_directory(self, tmp_path):
        # Arrange
        spec = _write_spec(tmp_path, {"runtime": "apptainer"}, name="auto-named")
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.name == "auto-named"


# ---------------------------------------------------------------------------
# spec.apptainer.* — engine-scoped block
# ---------------------------------------------------------------------------


class TestApptainerBlock:
    """Apptainer-scoped knobs that landed pre-realignment (overlay, nv,
    rocm, post, environment, def_file) — these already nest correctly."""

    def test_apptainer_overlay_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "apptainer": {"overlay": "./ovl.img"}},
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.apptainer.overlay == "./ovl.img"

    def test_apptainer_nv_flag_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path, {"runtime": "apptainer", "apptainer": {"nv": True}}
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.apptainer.nv is True

    def test_apptainer_rocm_flag_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path, {"runtime": "apptainer", "apptainer": {"rocm": True}}
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.apptainer.rocm is True


class TestApptainerBlockGap:
    """Apptainer-scoped knobs the §3 realignment will move from the
    top level into ``spec.apptainer.*``. xfail until the loader is
    updated; flipping XPASS = remove the marker + tighten the schema.
    """

    def test_apptainer_image_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"image": "/path/to/sac.sif"},
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.apptainer.image == "/path/to/sac.sif"

    def test_apptainer_binds_round_trip(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"binds": ["/data:/data:ro"]},
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert "/data:/data:ro" in cfg.apptainer.binds

    def test_apptainer_env_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"env": {"FOO": "bar"}},
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.apptainer.env.get("FOO") == "bar"

    def test_apptainer_raw_args_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"raw_args": ["--userns", "--cleanenv"]},
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.apptainer.raw_args == ["--userns", "--cleanenv"]


# ---------------------------------------------------------------------------
# spec.claude.* — session-scoped block
# ---------------------------------------------------------------------------


class TestClaudeBlock:
    """Session-scoped knobs that landed pre-realignment."""

    def test_claude_channels_round_trip(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "claude": {"channels": ["server:hub-push"]},
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert "server:hub-push" in cfg.claude.channels

    def test_claude_flags_round_trip(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "claude": {"flags": ["--dangerously-skip-permissions"]},
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert "--dangerously-skip-permissions" in cfg.claude.flags

    def test_claude_session_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "claude": {"session": "continue"}},
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.claude.session == "continue"


class TestClaudeBlockGap:
    """§3 realignment gap — model moves from top-level into
    `spec.claude.model`; raw_options escape hatch lands fresh."""

    def test_claude_model_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "claude": {"model": "opus"}},
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.claude.model == "opus"

    def test_claude_raw_options_round_trips(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "claude": {"raw_options": {"max_turns": 50}},
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.claude.raw_options.get("max_turns") == 50


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------


class TestStartup:
    def test_startup_commands_command_round_trips(self, tmp_path):
        """`startup_commands` are shell commands run BEFORE claude starts."""
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "startup_commands": [
                    {"command": "echo hello"},
                ],
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert — single check is enough: a non-empty list whose first
        # entry carries the right command string proves the round-trip.
        assert cfg.startup_commands and cfg.startup_commands[0].command == "echo hello"

    def test_startup_prompts_round_trip(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "startup_prompts": ["Apply the SciTeX quality playbook."],
            },
        )
        # Act
        cfg = load_config(str(spec))
        # Assert
        assert cfg.startup_prompts == ["Apply the SciTeX quality playbook."]


# ---------------------------------------------------------------------------
# Removed fields — these MUST fail validation per §3 -> Removed from v3
# ---------------------------------------------------------------------------


class TestRemovedFields:
    """§3 -> "Removed from v3" — these fields are explicitly gone.
    xfail(strict=True): the validator currently still accepts them;
    the realignment commits should make these tests pass (by
    rejecting the field) and the xfail markers should then be lifted.
    """

    def test_spec_skills_is_rejected_by_validator(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "skills": {"required": ["foo"]}},
        )
        # Act
        errors = validate_config(str(spec))
        # Assert
        assert any("skills" in e for e in errors), (
            "spec.skills should fail validation after v3 realignment"
        )

    def test_spec_remote_is_rejected_by_validator(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "remote": {"host": "spartan"}},
        )
        # Act
        errors = validate_config(str(spec))
        # Assert
        assert any("remote" in e for e in errors), (
            "spec.remote should fail validation after v3 realignment"
        )

    def test_top_level_spec_image_is_rejected(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "image": "/path/to/sac.sif"},
        )
        # Act
        errors = validate_config(str(spec))
        # Assert
        assert any("image" in e and "apptainer" in e for e in errors), (
            "top-level spec.image should fail validation once "
            "spec.apptainer.image is the canonical home"
        )

    def test_top_level_spec_mounts_is_rejected(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "mounts": [{"src": "/a", "dst": "/b"}]},
        )
        # Act
        errors = validate_config(str(spec))
        # Assert
        assert any("mounts" in e for e in errors)

    def test_top_level_spec_env_is_rejected(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "env": {"FOO": "bar"}},
        )
        # Act
        errors = validate_config(str(spec))
        # Assert
        assert any("env" in e for e in errors)

    def test_top_level_spec_model_is_rejected(self, tmp_path):
        # Arrange
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "model": "opus"},
        )
        # Act
        errors = validate_config(str(spec))
        # Assert
        assert any("model" in e for e in errors)


# EOF
