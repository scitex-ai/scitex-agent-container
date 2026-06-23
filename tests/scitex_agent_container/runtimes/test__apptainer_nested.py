"""Mirror test for ``runtimes/_apptainer_nested.py`` (PS-204 §2).

``nested_build_flags`` enables a solver to build/pull a capsule's pinned
environment from inside its own SAC apptainer container (the
``spec.apptainer.nested_build`` knob). It emits ``--bind /dev/fuse`` +
empty-file masks over ``/etc/subuid``/``/etc/subgid`` (→ root-mapped +
``fakeroot``-command build path, no setuid ``newuidmap``) + the
``APPTAINER_TMPDIR``/``CACHEDIR`` real-disk env.

Real AgentConfig via ``load_config`` on a tmp spec — no mocks (PA-306).
The flag set is host-FUSE-gated by design (the helper fails loud when
``/dev/fuse`` is absent), so the ON-path assertions ``skipif`` on a host
without ``/dev/fuse``. STX-TQ002 AAA-marker + one observable assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_nested import nested_build_flags

_HAS_FUSE = Path("/dev/fuse").exists()
_FUSE_SKIP = pytest.mark.skipif(
    not _HAS_FUSE, reason="nested_build requires /dev/fuse on the test host"
)

_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  host: local
  workdir: /tmp/agt-work
  apptainer:
    image: /x.sif
    binds: []
{extra}
  claude:
    model: claude-opus-4-8[1m]
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
"""


def _cfg(tmp_path: Path, extra: str):
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(_SPEC.format(extra=extra), encoding="utf-8")
    return load_config(str(spec))


_ON = "    nested_build: true"


class TestNestedBuildFlags:
    def test_off_returns_empty(self, tmp_path):
        # Arrange — no apptainer.nested_build.
        config = _cfg(tmp_path, "")
        # Act
        flags = nested_build_flags(config, tmp_path / "state")
        # Assert
        assert flags == []

    @_FUSE_SKIP
    def test_on_binds_dev_fuse(self, tmp_path):
        # Arrange
        config = _cfg(tmp_path, _ON)
        # Act
        flags = nested_build_flags(config, tmp_path / "state")
        # Assert
        assert "/dev/fuse" in flags

    @_FUSE_SKIP
    def test_on_masks_subuid_and_subgid(self, tmp_path):
        # Arrange
        config = _cfg(tmp_path, _ON)
        # Act
        joined = " ".join(nested_build_flags(config, tmp_path / "state"))
        # Assert — one empty file bound over BOTH subuid + subgid.
        assert ":/etc/subuid" in joined and ":/etc/subgid" in joined

    @_FUSE_SKIP
    def test_on_points_tmpdir_at_real_disk_tmp(self, tmp_path):
        # Arrange
        config = _cfg(tmp_path, _ON)
        # Act
        flags = nested_build_flags(config, tmp_path / "state")
        # Assert
        assert "APPTAINER_TMPDIR=/tmp" in flags

    @_FUSE_SKIP
    def test_on_creates_mask_file_on_disk(self, tmp_path):
        # Arrange — the binds reference an empty file that must exist.
        config = _cfg(tmp_path, _ON)
        state = tmp_path / "state"
        # Act
        nested_build_flags(config, state)
        # Assert
        assert (state / "nested-build" / "empty").is_file()
