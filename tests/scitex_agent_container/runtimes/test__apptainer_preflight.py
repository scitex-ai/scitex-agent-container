"""D2 — static $HOME-visibility preflight tests.

See docs/design/2026-05-13-isolation-hardening.md (D2 + D4).
"""

from __future__ import annotations

import shlex
from pathlib import Path

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._apptainer_preflight import PREFLIGHT_SCRIPT
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


def _inner_after_sif(argv: list[str], sif: Path) -> list[str]:
    return argv[argv.index(str(sif)) + 1 :]


def test_preflight_wraps_inner_cmd_by_default(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    cfg = _cfg(tmp_path, startup_prompts=["go"])
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    inner = _inner_after_sif(argv, sif)
    # Wrapped: ["bash", "-c", "<preflight>\nexec <inner_quoted>"]
    assert inner[0] == "bash"
    assert inner[1] == "-c"
    script = inner[2]
    # preflight signatures
    assert 'test "$(id -u)" != "0"' in script
    assert 'test ! -d "$HOME"' in script
    # Inner is exec'd so PID 1 is tini, not bash
    assert "\nexec " in script
    assert "/usr/bin/tini" in script


def test_preflight_skipped_when_relaxed(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(relaxed=True), startup_prompts=["go"])
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    inner = _inner_after_sif(argv, sif)
    # No wrapper: inner starts directly with tini
    assert inner[0] == "/usr/bin/tini"
    # No preflight strings anywhere in argv
    joined = "\n".join(argv)
    assert 'test ! -d "$HOME"' not in joined


def test_preflight_quotes_embedded_specials_safely(tmp_path: Path) -> None:
    """Mission strings with quotes / spaces / $ / semicolons must round-trip
    through the bash -c wrapper without breakage."""
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    tricky = "hello 'world'; echo $PATH \"oops\""
    cfg = _cfg(tmp_path, startup_prompts=[tricky])
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    inner = _inner_after_sif(argv, sif)
    assert inner[0:2] == ["bash", "-c"]
    script = inner[2]
    _, _, exec_line = script.rpartition("\nexec ")
    parsed = shlex.split(exec_line)
    # Mission must round-trip intact.
    assert tricky in parsed
    assert "/usr/bin/tini" in parsed


def test_preflight_constant_is_static() -> None:
    """The preflight is a module constant (no operator-specific generation
    — see ADR §D4: static = single sha256, verifiable by Clew)."""
    assert isinstance(PREFLIGHT_SCRIPT, str)
    assert "test ! -d" in PREFLIGHT_SCRIPT
    assert "id -u" in PREFLIGHT_SCRIPT


def test_preflight_present_when_overlay_set(tmp_path: Path) -> None:
    """Overlay doesn't disable preflight — only --writable-tmpfs is skipped."""
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    overlay = tmp_path / "ov.img"
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(overlay=str(overlay)))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    inner = _inner_after_sif(argv, sif)
    assert inner[0:2] == ["bash", "-c"]
    assert 'test ! -d "$HOME"' in inner[2]
