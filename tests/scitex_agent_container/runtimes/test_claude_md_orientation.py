"""Tests for the launch-time orientation block in ``runtimes.claude_md``.

PA-306 no-mocks. The fixture is a REAL minimal v3 spec written to
``tmp_path`` (dir-as-SSoT: ``<name>/spec.yaml``) and loaded through the
real ``config.load_config`` — the same path the launcher takes — so the
orientation block is asserted against genuinely loaded spec values, not
hand-built dataclasses. A bare ``AgentConfig`` covers the unset-role
rendering (mirrors the ``_cfg`` builder in ``test_claude_md.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml
from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes.claude_md import (
    ORIENTATION_MAX_LINES,
    build_orientation_lines,
    setup_claude_md,
)

# Mirrors examples/agents/minimal-agent/spec.yaml (every REQUIRED field,
# nothing more) + the identity labels the orientation block renders.
_MINIMAL_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels:
    role: orient-worker
    purpose: verify orientation rendering

spec:
  runtime: apptainer
  host: ${HOSTNAME}
  workdir: /home/agent/work

  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
    binds: []

  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions

  health:
    enabled: true
    interval: 60

  restart:
    policy: never
    max_retries: 0
"""


@pytest.fixture
def loaded_config(tmp_path):
    """Real AgentConfig loaded from a real minimal spec.yaml fixture."""
    agent_dir = tmp_path / "orient-fixture"
    agent_dir.mkdir()
    (agent_dir / "spec.yaml").write_text(explicitize_yaml(_MINIMAL_SPEC))
    return load_config(agent_dir / "spec.yaml")


def _generated_text(config, tmp_path) -> str:
    setup_claude_md(config, str(tmp_path))
    return (Path(tmp_path) / ".claude" / "CLAUDE.md").read_text()


def _orientation_section(text: str) -> list[str]:
    """Return the orientation block's lines (heading through last row)."""
    lines = text.splitlines()
    start = lines.index("### Orientation")
    section = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith(("###", "##", "<!--")):
            break
        if line.strip():
            section.append(line)
    return section


# ---------------------------------------------------------------------------
# Rendering from a really-loaded spec
# ---------------------------------------------------------------------------


def test_orientation_section_present(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "### Orientation" in text


def test_orientation_where_line_has_workdir(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "workdir=/home/agent/work" in text


def test_orientation_where_line_has_image_basename(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "image=sac-base.sif" in text


def test_orientation_run_as_has_runtime(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "runtime=apptainer" in text


def test_orientation_run_as_has_model(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "model=haiku" in text


def test_orientation_run_as_has_restart_policy(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "restart=never" in text


def test_orientation_role_from_labels(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "role: orient-worker" in text


def test_orientation_purpose_from_labels(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "purpose: verify orientation rendering" in text


def test_orientation_points_to_whoami(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "sac whoami" in text


def test_orientation_names_the_sac_skill(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert "scitex-agent-container" in _orientation_section(text)[-1]


# ---------------------------------------------------------------------------
# Line budget — the block must stay scannable
# ---------------------------------------------------------------------------


def test_orientation_section_within_line_budget(loaded_config, tmp_path):
    # Arrange (fixture)
    # Act
    text = _generated_text(loaded_config, tmp_path)
    # Assert
    assert len(_orientation_section(text)) <= ORIENTATION_MAX_LINES


def test_build_orientation_lines_within_budget(loaded_config):
    # Arrange (fixture)
    # Act
    lines = build_orientation_lines(loaded_config)
    # Assert
    assert len(lines) <= ORIENTATION_MAX_LINES


# ---------------------------------------------------------------------------
# Unset-role rendering (bare AgentConfig — no labels at all)
# ---------------------------------------------------------------------------


def test_orientation_unset_role_renders_unset_marker():
    # Arrange
    cfg = AgentConfig(name="bare")
    # Act
    lines = build_orientation_lines(cfg)
    # Assert
    assert any("role: (unset)" in line for line in lines)


def test_orientation_unset_role_avoids_capital_role_token(tmp_path):
    # Arrange: the legacy claude_md test asserts a no-role config emits no
    # "Role:" token anywhere in CLAUDE.md — the orientation block must not
    # reintroduce it.
    cfg = AgentConfig(name="bare")
    # Act
    text = _generated_text(cfg, tmp_path)
    # Assert
    assert "Role:" not in text


def test_orientation_default_image_placeholder():
    # Arrange
    cfg = AgentConfig(name="bare")
    # Act
    lines = build_orientation_lines(cfg)
    # Assert
    assert any("image=(default sac SIF)" in line for line in lines)
