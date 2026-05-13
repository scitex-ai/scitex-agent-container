"""D1 — auto-prepend of --containall / --cleanenv / --writable-tmpfs.

See docs/design/2026-05-13-isolation-hardening.md.

Kept in a separate file so ``test__apptainer_runtime.py`` doesn't grow
past sac's 512-line per-file cap (it's already over due to legacy
consolidation; new tests land here).
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._apptainer_runtime import (
    ApptainerContainerRuntime,
)


def _cfg(workdir: Path, **kw) -> AgentConfig:
    return AgentConfig(
        name=kw.pop("name", "iso"),
        runtime="apptainer",
        workdir=str(workdir),
        **kw,
    )


# ---------------------------------------------------------------------------
# D1 — --cleanenv auto-prepend
# ---------------------------------------------------------------------------


def test_cleanenv_present_by_default(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec())
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--cleanenv" in argv
    assert argv.index("--cleanenv") < argv.index(str(tmp_path / "x.sif"))


def test_cleanenv_absent_when_relaxed_true(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(relaxed=True))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--cleanenv" not in argv


def test_cleanenv_not_doubled_when_operator_set(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(raw_args=["--cleanenv"]))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert argv.count("--cleanenv") == 1


# ---------------------------------------------------------------------------
# D1 — --writable-tmpfs auto-prepend (only when no overlay)
# ---------------------------------------------------------------------------


def test_writable_tmpfs_present_by_default(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec())
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--writable-tmpfs" in argv


def test_writable_tmpfs_absent_when_overlay_set(tmp_path: Path) -> None:
    """apptainer rejects --writable-tmpfs + --overlay simultaneously."""
    rt = ApptainerContainerRuntime()
    overlay = tmp_path / "ov.img"
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(overlay=str(overlay)))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--writable-tmpfs" not in argv
    assert "--overlay" in argv


def test_writable_tmpfs_absent_when_relaxed(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(relaxed=True))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--writable-tmpfs" not in argv


def test_writable_tmpfs_not_doubled_when_operator_set(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(raw_args=["--writable-tmpfs"]))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert argv.count("--writable-tmpfs") == 1


def test_all_three_hardening_flags_coexist(tmp_path: Path) -> None:
    """relaxed=false + no overlay + no operator overrides → all three flags."""
    rt = ApptainerContainerRuntime()
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec())
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--containall" in argv
    assert "--cleanenv" in argv
    assert "--writable-tmpfs" in argv
