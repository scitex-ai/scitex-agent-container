"""Tests for relaxed-overlay ``to_home`` delivery (:mod:`_to_home_overlay`).

Relaxed apptainer specs declare ``--containall --home /home/agent
--overlay <dir>`` in raw_args; under that combo the workspace-home bind is
shadowed and the ``to_home`` tree must be mirrored into the overlay's upper
home (``<overlay>/upper/<container_home>/``) so it reaches the container
``$HOME``.

PA-306 no-mocks: real ``AgentConfig`` + ``ApptainerSpec`` instances against
``tmp_path`` real directories. No monkeypatching — the resolvers are pure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._to_home_overlay import (
    DEFAULT_CONTAINER_HOME,
    deploy_to_home_overlay,
    resolve_container_home,
    resolve_overlay_upper_home,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _relaxed_cfg(
    tmp_path: Path,
    *,
    overlay_dir: Path | None = None,
    container_home: str = "/home/agent",
    seed_to_home: bool = True,
) -> AgentConfig:
    """Build a relaxed apptainer ``AgentConfig`` whose raw_args declare
    ``--home <container_home> --overlay <overlay_dir>``, with a populated
    ``to_home/`` next to spec.yaml.
    """
    agent_dir = tmp_path / "agent_def"
    agent_dir.mkdir(parents=True, exist_ok=True)
    if seed_to_home:
        th = agent_dir / "to_home"
        (th / ".claude").mkdir(parents=True, exist_ok=True)
        (th / ".claude" / "settings.local.json").write_text('{"hooks": {}}\n')
        (th / ".bashrc").write_text("export FROM_TO_HOME=1\n")
    if overlay_dir is None:
        overlay_dir = tmp_path / "overlay"
    raw_args = [
        "--containall",
        "--home",
        container_home,
        "--overlay",
        str(overlay_dir),
    ]
    cfg = AgentConfig(name="relaxed-agent", runtime="apptainer", workdir=str(tmp_path))
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.to_home = ""
    cfg.apptainer = ApptainerSpec(relaxed=True, raw_args=raw_args)
    return cfg


# ---------------------------------------------------------------------------
# resolve_container_home
# ---------------------------------------------------------------------------


class TestResolveContainerHome:
    def test_reads_home_from_raw_args(self, tmp_path):
        # Arrange
        cfg = _relaxed_cfg(tmp_path, container_home="/home/agent")
        # Act
        # Assert
        assert resolve_container_home(cfg) == "/home/agent"

    def test_honours_non_default_home_override(self, tmp_path):
        # Arrange
        cfg = _relaxed_cfg(tmp_path, container_home="/home/custom")
        # Act
        # Assert
        assert resolve_container_home(cfg) == "/home/custom"

    def test_defaults_when_no_home_in_raw_args(self, tmp_path):
        # Arrange — apptainer spec with no --home flag.
        cfg = AgentConfig(name="a", runtime="apptainer", workdir=str(tmp_path))
        cfg.apptainer = ApptainerSpec(raw_args=["--containall"])
        # Act
        # Assert
        assert resolve_container_home(cfg) == DEFAULT_CONTAINER_HOME


# ---------------------------------------------------------------------------
# resolve_overlay_upper_home
# ---------------------------------------------------------------------------


class TestResolveOverlayUpperHome:
    def test_joins_overlay_upper_and_container_home(self, tmp_path):
        # Arrange
        overlay = tmp_path / "ov"
        cfg = _relaxed_cfg(tmp_path, overlay_dir=overlay, container_home="/home/agent")
        # Act
        dest = resolve_overlay_upper_home(cfg)
        # Assert
        assert dest == overlay / "upper" / "home" / "agent"

    def test_reads_overlay_from_modeled_field(self, tmp_path):
        # Arrange — overlay declared via the modeled spec.apptainer.overlay
        # field rather than raw_args.
        overlay = tmp_path / "ov2"
        overlay.mkdir()
        cfg = AgentConfig(name="a", runtime="apptainer", workdir=str(tmp_path))
        cfg.apptainer = ApptainerSpec(
            relaxed=True, overlay=str(overlay), raw_args=["--home", "/home/agent"]
        )
        # Act
        dest = resolve_overlay_upper_home(cfg)
        # Assert
        assert dest == overlay / "upper" / "home" / "agent"

    def test_returns_none_without_overlay(self, tmp_path):
        # Arrange — no overlay declared anywhere.
        cfg = AgentConfig(name="a", runtime="apptainer", workdir=str(tmp_path))
        cfg.apptainer = ApptainerSpec(raw_args=["--home", "/home/agent"])
        # Act
        # Assert
        assert resolve_overlay_upper_home(cfg) is None

    def test_returns_none_for_img_overlay_file(self, tmp_path):
        # Arrange — an .img loopback overlay (a file) cannot host an upper/.
        img = tmp_path / "overlay.img"
        img.write_bytes(b"\x00")
        cfg = AgentConfig(name="a", runtime="apptainer", workdir=str(tmp_path))
        cfg.apptainer = ApptainerSpec(
            relaxed=True, raw_args=["--home", "/home/agent", "--overlay", str(img)]
        )
        # Act
        # Assert
        assert resolve_overlay_upper_home(cfg) is None

    def test_relative_overlay_resolves_against_workdir(self, tmp_path):
        # Arrange — relative --overlay path resolves against spec.workdir.
        cfg = AgentConfig(name="a", runtime="apptainer", workdir=str(tmp_path))
        cfg.apptainer = ApptainerSpec(
            relaxed=True, raw_args=["--home", "/home/agent", "--overlay", "ov_rel"]
        )
        # Act
        dest = resolve_overlay_upper_home(cfg)
        # Assert
        assert dest == tmp_path / "ov_rel" / "upper" / "home" / "agent"


# ---------------------------------------------------------------------------
# deploy_to_home_overlay — real file placement
# ---------------------------------------------------------------------------


@pytest.fixture
def deployed_overlay(tmp_path: Path) -> tuple[Path, Path]:
    """Run a real overlay deploy once; return ``(overlay_dir, dest)``."""
    overlay = tmp_path / "ov"
    cfg = _relaxed_cfg(tmp_path, overlay_dir=overlay, container_home="/home/agent")
    dest = deploy_to_home_overlay(cfg)
    assert dest is not None
    return overlay, dest


class TestDeployToHomeOverlay:
    def test_destination_is_overlay_upper_home(self, deployed_overlay):
        # Arrange
        overlay, dest = deployed_overlay
        # Act
        # Assert
        assert dest == overlay / "upper" / "home" / "agent"

    def test_non_claude_file_lands_one_to_one(self, deployed_overlay):
        # Arrange — delivery is GENERAL: .bashrc, not just .claude.
        _, dest = deployed_overlay
        # Act
        # Assert
        assert (dest / ".bashrc").read_text() == "export FROM_TO_HOME=1\n"

    def test_settings_file_lands_under_claude(self, deployed_overlay):
        # Arrange — the cascade normalizes to the USER-scope name
        # settings.json (ADR-0018); the overlay home IS the container $HOME.
        _, dest = deployed_overlay
        # Act
        # Assert
        assert (dest / ".claude" / "settings.json").is_file()

    def test_settings_relpath_matches_in_container_path(self, deployed_overlay):
        # Arrange — relative path under upper must match the in-container
        # /home/agent/.claude/settings.json the TUI loads at USER scope.
        overlay, dest = deployed_overlay
        # Act
        rel = (dest / ".claude" / "settings.json").relative_to(overlay / "upper")
        # Assert
        assert str(rel) == "home/agent/.claude/settings.json"

    def test_creates_upper_home_dir_when_overlay_absent(self, tmp_path):
        # Arrange — overlay dir doesn't exist yet (apptainer creates upper/
        # on first launch; we pre-create it for the host-side write).
        overlay = tmp_path / "fresh_overlay"
        cfg = _relaxed_cfg(tmp_path, overlay_dir=overlay)
        # Act
        dest = deploy_to_home_overlay(cfg)
        # Assert
        assert dest.is_dir()

    def test_writes_files_when_overlay_absent(self, tmp_path):
        # Arrange
        overlay = tmp_path / "fresh_overlay2"
        cfg = _relaxed_cfg(tmp_path, overlay_dir=overlay)
        # Act
        dest = deploy_to_home_overlay(cfg)
        # Assert
        assert (dest / ".bashrc").is_file()

    def test_noop_returns_none_for_non_overlay_spec(self, tmp_path):
        # Arrange — relaxed spec but no overlay → workspace-home bind handles
        # delivery; overlay path is a no-op.
        cfg = AgentConfig(name="a", runtime="apptainer", workdir=str(tmp_path))
        agent_dir = tmp_path / "agent_def"
        (agent_dir / "to_home").mkdir(parents=True)
        (agent_dir / "to_home" / ".bashrc").write_text("x\n")
        cfg.config_path = str(agent_dir / "spec.yaml")
        cfg.apptainer = ApptainerSpec(relaxed=True, raw_args=["--home", "/home/agent"])
        # Act
        # Assert
        assert deploy_to_home_overlay(cfg) is None
