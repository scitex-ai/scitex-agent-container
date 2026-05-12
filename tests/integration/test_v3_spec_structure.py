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
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from scitex_agent_container.config import load_config, validate_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
                "spec": spec_body,
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
        spec = _write_spec(tmp_path, {"runtime": "apptainer"})
        cfg = load_config(str(spec))
        assert cfg.runtime == "apptainer"

    def test_workdir_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path, {"runtime": "apptainer", "workdir": "/tmp/agent-x"}
        )
        cfg = load_config(str(spec))
        assert cfg.workdir == "/tmp/agent-x"

    def test_a2a_port_round_trips(self, tmp_path):
        spec = _write_spec(tmp_path, {"runtime": "apptainer", "a2a": {"port": 7901}})
        cfg = load_config(str(spec))
        assert cfg.a2a.port == 7901

    def test_health_block_round_trips(self, tmp_path):
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
        cfg = load_config(str(spec))
        assert cfg.health.enabled is True
        assert cfg.health.interval == 60
        assert cfg.health.method == "sdk-alive"

    def test_restart_block_round_trips(self, tmp_path):
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
        cfg = load_config(str(spec))
        assert cfg.restart.policy == "on-failure"
        assert cfg.restart.max_retries == 3
        assert cfg.restart.backoff_initial == 30

    def test_metadata_labels_round_trip(self, tmp_path):
        agent_dir = tmp_path / "labelled"
        agent_dir.mkdir()
        spec_path = agent_dir / "spec.yaml"
        spec_path.write_text(
            _yaml.safe_dump(
                {
                    "apiVersion": "scitex-agent-container/v3",
                    "kind": "Agent",
                    "metadata": {"labels": {"role": "researcher", "team": "lab-a"}},
                    "spec": {"runtime": "apptainer"},
                }
            )
        )
        cfg = load_config(str(spec_path))
        assert cfg.labels.get("role") == "researcher"
        assert cfg.labels.get("team") == "lab-a"


class TestDirAsSSoT:
    """§3 — there's no ``metadata.name`` field; the agent name is the
    parent directory of spec.yaml."""

    def test_name_derived_from_parent_directory(self, tmp_path):
        spec = _write_spec(tmp_path, {"runtime": "apptainer"}, name="auto-named")
        cfg = load_config(str(spec))
        assert cfg.name == "auto-named"


# ---------------------------------------------------------------------------
# spec.apptainer.* — engine-scoped block
# ---------------------------------------------------------------------------


class TestApptainerBlock:
    """Apptainer-scoped knobs that landed pre-realignment (overlay, nv,
    rocm, post, environment, def_file) — these already nest correctly."""

    def test_overlay_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "apptainer": {"overlay": "./ovl.img"}},
        )
        cfg = load_config(str(spec))
        assert cfg.apptainer.overlay == "./ovl.img"

    def test_nv_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path, {"runtime": "apptainer", "apptainer": {"nv": True}}
        )
        cfg = load_config(str(spec))
        assert cfg.apptainer.nv is True

    def test_rocm_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path, {"runtime": "apptainer", "apptainer": {"rocm": True}}
        )
        cfg = load_config(str(spec))
        assert cfg.apptainer.rocm is True


class TestApptainerBlockGap:
    """Apptainer-scoped knobs the §3 realignment will move from the
    top level into ``spec.apptainer.*``. xfail until the loader is
    updated; flipping XPASS = remove the marker + tighten the schema.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.apptainer.image not yet "
        "promoted from top-level spec.image",
    )
    def test_apptainer_image_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"image": "/path/to/sac.sif"},
            },
        )
        cfg = load_config(str(spec))
        assert cfg.apptainer.image == "/path/to/sac.sif"

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.apptainer.binds[] not yet "
        "promoted from top-level spec.mounts[]",
    )
    def test_apptainer_binds_round_trip(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"binds": ["/data:/data:ro"]},
            },
        )
        cfg = load_config(str(spec))
        assert "/data:/data:ro" in cfg.apptainer.binds

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.apptainer.env not yet "
        "promoted from top-level spec.env",
    )
    def test_apptainer_env_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"env": {"FOO": "bar"}},
            },
        )
        cfg = load_config(str(spec))
        assert cfg.apptainer.env.get("FOO") == "bar"

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.apptainer.raw_args escape "
        "hatch not yet implemented (§1 invariant)",
    )
    def test_apptainer_raw_args_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "apptainer": {"raw_args": ["--userns", "--cleanenv"]},
            },
        )
        cfg = load_config(str(spec))
        assert cfg.apptainer.raw_args == ["--userns", "--cleanenv"]


# ---------------------------------------------------------------------------
# spec.claude.* — session-scoped block
# ---------------------------------------------------------------------------


class TestClaudeBlock:
    """Session-scoped knobs that landed pre-realignment."""

    def test_channels_round_trip(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "claude": {"channels": ["server:orochi-push"]},
            },
        )
        cfg = load_config(str(spec))
        assert "server:orochi-push" in cfg.claude.channels

    def test_flags_round_trip(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "claude": {"flags": ["--dangerously-skip-permissions"]},
            },
        )
        cfg = load_config(str(spec))
        assert "--dangerously-skip-permissions" in cfg.claude.flags

    def test_session_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "claude": {"session": "continue"}},
        )
        cfg = load_config(str(spec))
        assert cfg.claude.session == "continue"


class TestClaudeBlockGap:
    """§3 realignment gap — model moves from top-level into
    `spec.claude.model`; raw_options escape hatch lands fresh."""

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.claude.model not yet "
        "promoted from top-level spec.model",
    )
    def test_claude_model_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "claude": {"model": "opus"}},
        )
        cfg = load_config(str(spec))
        assert cfg.claude.model == "opus"

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.claude.raw_options escape "
        "hatch not yet implemented (§1 invariant)",
    )
    def test_claude_raw_options_round_trips(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "claude": {"raw_options": {"max_turns": 50}},
            },
        )
        cfg = load_config(str(spec))
        assert cfg.claude.raw_options.get("max_turns") == 50


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------


class TestStartup:
    def test_startup_commands_round_trip(self, tmp_path):
        """`startup_commands` are shell commands run BEFORE claude starts."""
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "startup_commands": [
                    {"command": "echo hello"},
                ],
            },
        )
        cfg = load_config(str(spec))
        assert cfg.startup_commands
        assert cfg.startup_commands[0].command == "echo hello"

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.startup_prompts (separate "
        "from startup_commands) per §3 — fed to Claude as first "
        "user message(s)",
    )
    def test_startup_prompts_round_trip(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {
                "runtime": "apptainer",
                "startup_prompts": ["Apply the SciTeX quality playbook."],
            },
        )
        cfg = load_config(str(spec))
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

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.skills must be rejected — "
        "skills now live in dot_claude/skills/ (§3 Removed)",
    )
    def test_spec_skills_is_rejected(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "skills": {"required": ["foo"]}},
        )
        errors = validate_config(str(spec))
        assert any("skills" in e for e in errors), (
            "spec.skills should fail validation after v3 realignment"
        )

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: spec.remote must be rejected — "
        "cross-host is orochi's job (§2)",
    )
    def test_spec_remote_is_rejected(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "remote": {"host": "spartan"}},
        )
        errors = validate_config(str(spec))
        assert any("remote" in e for e in errors), (
            "spec.remote should fail validation after v3 realignment"
        )

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: top-level spec.image must be "
        "rejected once promoted into spec.apptainer.image",
    )
    def test_top_level_spec_image_is_rejected(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "image": "/path/to/sac.sif"},
        )
        errors = validate_config(str(spec))
        assert any("image" in e and "apptainer" in e for e in errors), (
            "top-level spec.image should fail validation once "
            "spec.apptainer.image is the canonical home"
        )

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: top-level spec.mounts must be "
        "rejected once promoted into spec.apptainer.binds",
    )
    def test_top_level_spec_mounts_is_rejected(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "mounts": [{"src": "/a", "dst": "/b"}]},
        )
        errors = validate_config(str(spec))
        assert any("mounts" in e for e in errors)

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: top-level spec.env must be rejected "
        "once promoted into spec.apptainer.env",
    )
    def test_top_level_spec_env_is_rejected(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "env": {"FOO": "bar"}},
        )
        errors = validate_config(str(spec))
        assert any("env" in e for e in errors)

    @pytest.mark.xfail(
        strict=True,
        reason="v3-realign pending: top-level spec.model must be rejected "
        "once promoted into spec.claude.model",
    )
    def test_top_level_spec_model_is_rejected(self, tmp_path):
        spec = _write_spec(
            tmp_path,
            {"runtime": "apptainer", "model": "opus"},
        )
        errors = validate_config(str(spec))
        assert any("model" in e for e in errors)


# EOF
