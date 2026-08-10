"""The hook-floor gate runs on the CONTAINER's own startup path.

This is the placement decision the whole card turns on, so it is pinned by
test rather than left to a comment. ``agent_start`` runs on the BARE HOST,
where the only ``$HOME`` it can read is the operator's — measured 2026-08-10, a
host-side layer read reported 67 pre-tool-use hooks and called
``log_post_tool_use.sh`` missing while the container had 71 and the hook. So
the gate is emitted into the in-container ``bash -lc`` chain instead, BEFORE
``exec``.

PA-306 no-mocks: real ``AgentConfig`` objects from the real loader, and the
real ``build_inner_argv``.
STX-TQ002/TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    build_inner_argv,
    hook_floor_gate_steps,
)

from .._helpers.explicit_spec import explicit_doc

_FLOOR = {"pre-tool-use": ["enforce_git_dash_C.sh"]}


def _config(tmp_path: Path, overrides: dict):
    spec = {"host": "${HOSTNAME}", **overrides}
    agent_dir = tmp_path / "agents" / "gate-fixture"
    agent_dir.mkdir(parents=True)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(explicit_doc(spec), sort_keys=False))
    return load_config(str(path))


@pytest.fixture
def declared(tmp_path: Path):
    return _config(tmp_path, {"required_claude_hooks": _FLOOR})


@pytest.fixture
def undeclared(tmp_path: Path):
    return _config(tmp_path, {})


class TestTheGateIsOptInPerSpec:
    def test_undeclared_spec_gets_no_gate_step(self, undeclared):
        # Arrange — most specs. No step means no refusal AND no line to scroll
        # past, which is what "no warning spam" has to mean structurally.
        # Act
        steps = hook_floor_gate_steps(undeclared)
        # Assert
        assert steps == []

    def test_declared_spec_gets_exactly_one_gate_step(self, declared):
        # Arrange
        # Act
        steps = hook_floor_gate_steps(declared)
        # Assert
        assert len(steps) == 1

    def test_the_step_runs_the_in_container_hooks_verb(self, declared):
        # Arrange
        # Act
        step = hook_floor_gate_steps(declared)[0]
        # Assert
        assert "agents hooks" in step

    def test_the_step_names_the_agent(self, declared):
        # Arrange — the verb defaults to $SAC_NAME, but naming it explicitly
        # makes the boot line self-describing in a pane capture.
        # Act
        step = hook_floor_gate_steps(declared)[0]
        # Assert
        assert declared.name in step

    def test_a_failing_step_aborts_before_exec(self, declared):
        # Arrange — `|| exit 1` is explicit, NOT inherited from `set -e`: the
        # git-alias steps run before any `set -e`, and a spec with no
        # startup_commands never emits one at all.
        # Act
        step = hook_floor_gate_steps(declared)[0]
        # Assert
        assert step.endswith("|| exit 1")


class TestTheGateReachesTheRealInnerArgv:
    def test_declared_spec_puts_the_gate_in_the_inner_command(self, declared):
        # Arrange
        # Act
        argv = build_inner_argv(declared)
        # Assert
        assert "agents hooks" in argv[-1]

    def test_undeclared_spec_leaves_the_inner_command_untouched(self, undeclared):
        # Arrange
        # Act
        argv = build_inner_argv(undeclared)
        # Assert
        assert "agents hooks" not in argv[-1]

    def test_the_gate_precedes_the_runner_exec(self, declared):
        # Arrange — an agent whose guards are missing must never reach `exec`.
        inline = build_inner_argv(declared)[-1]
        # Act
        gate_first = inline.index("agents hooks") < inline.index("exec ")
        # Assert
        assert gate_first is True

    def test_the_gate_precedes_the_specs_own_startup_commands(self, tmp_path: Path):
        # Arrange — nor should it reach the spec's own bootstrap.
        config = _config(
            tmp_path,
            {
                "required_claude_hooks": _FLOOR,
                "startup_commands": [{"command": "echo MARKER_STARTUP"}],
            },
        )
        inline = build_inner_argv(config)[-1]
        # Act
        gate_first = inline.index("agents hooks") < inline.index("MARKER_STARTUP")
        # Assert
        assert gate_first is True
