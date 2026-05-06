"""Tests for ApptainerContainerRuntime (F-CS18)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec, StartupCommand
from scitex_agent_container.runtimes._apptainer_runtime import (
    APPTAINER_PID_FILE,
    ApptainerContainerRuntime,
)


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox the per-agent state-dir root."""
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    return root


def _config(workdir: Path, **kw) -> AgentConfig:
    return AgentConfig(
        name=kw.pop("name", "alpha"),
        runtime="apptainer",
        workdir=str(workdir),
        **kw,
    )


# ---------------------------------------------------------------------------
# build_run_argv shape
# ---------------------------------------------------------------------------


def test_argv_starts_with_apptainer_exec(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sif = tmp_path / "x.sif"
    argv = rt.build_run_argv(cfg, state_dir=tmp_path / "state", sif_path=sif)
    assert argv[0:2] == ["apptainer", "exec"]


def test_argv_emits_bind_mounts_in_apptainer_syntax(tmp_path: Path) -> None:
    """Bind syntax is `--bind src:dst` (not docker's `--mount type=bind,...`)."""
    rt = ApptainerContainerRuntime()
    workdir = tmp_path / "wd"
    state_dir = tmp_path / "state"
    cfg = _config(workdir)
    sif = tmp_path / "x.sif"
    argv = rt.build_run_argv(cfg, state_dir=state_dir, sif_path=sif)

    bind_idxs = [i for i, a in enumerate(argv) if a == "--bind"]
    binds = [argv[i + 1] for i in bind_idxs]
    assert any(b.endswith(":/work") and str(workdir) in b for b in binds)
    assert any(b.endswith(":/state") and str(state_dir) in b for b in binds)


def test_argv_sets_home_tmp(tmp_path: Path) -> None:
    """HOME=/tmp avoids the no-passwd-entry trap (mirrors docker path)."""
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "HOME=/tmp" in argv


def test_argv_does_not_emit_user_flag(tmp_path: Path) -> None:
    """apptainer runs as the calling UID by default; no --user flag."""
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--user" not in argv


def test_argv_runs_runner_module_via_tini(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        startup_commands=[StartupCommand(command="say hi")],
    )
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    sif_idx = argv.index(str(tmp_path / "x.sif"))
    inner = argv[sif_idx + 1 :]
    assert inner[0] == "/usr/bin/tini"
    assert "scitex_agent_container._runners.claude_session" in inner
    # Mission flows through the same way as docker.
    assert "--mission" in inner
    assert inner[inner.index("--mission") + 1] == "say hi"


def test_argv_forwards_sac_anthropic_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAC_ANTHROPIC_API_KEY", "sk-ant-api-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "SAC_ANTHROPIC_API_KEY=sk-ant-api-test" in argv


def test_argv_forwards_autonomous_block(tmp_path: Path) -> None:
    from scitex_agent_container.config._types import AutonomousSpec

    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        startup_commands=[StartupCommand(command="seed")],
        autonomous=AutonomousSpec(enabled=True, drive_until="OK", max_turns=7),
    )
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--autonomous-enabled" in argv
    assert argv[argv.index("--autonomous-drive-until") + 1] == "OK"
    assert argv[argv.index("--autonomous-max-turns") + 1] == "7"


# ---------------------------------------------------------------------------
# resolve_sif
# ---------------------------------------------------------------------------


def test_resolve_sif_uses_local_sif_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing .sif path is used directly; no build."""
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    cfg = _config(tmp_path, image=str(sif))
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._apptainer_runtime.shutil.which",
        lambda _: "/usr/bin/apptainer",
    )

    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    assert resolved == sif


def test_resolve_sif_returns_none_when_apptainer_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path, image=str(tmp_path / "x.sif"))
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._apptainer_runtime.shutil.which",
        lambda _: None,
    )
    assert ApptainerContainerRuntime().resolve_sif(cfg) is None


def test_resolve_sif_def_file_takes_precedence_over_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ApptainerSpec.def_file is set, sac builds from the .def
    even if spec.image points at an existing .sif."""
    existing_sif = tmp_path / "existing.sif"
    existing_sif.write_bytes(b"\x00")
    def_file = tmp_path / "extend.def"
    def_file.write_text("Bootstrap: docker\nFrom: python:3.11-slim\n")

    captured: dict = {}

    def fake_build_def(sif_path: Path, df: Path) -> bool:
        captured["sif"] = sif_path
        captured["def"] = df
        sif_path.write_bytes(b"\x00")
        return True

    monkeypatch.setattr(
        "scitex_agent_container.runtimes._apptainer_runtime.shutil.which",
        lambda _: "/usr/bin/apptainer",
    )
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._apptainer_runtime._build_sif_from_def",
        fake_build_def,
    )

    cfg = _config(
        tmp_path,
        image=str(existing_sif),
        apptainer=ApptainerSpec(def_file=str(def_file)),
    )
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    assert resolved is not None
    assert captured["def"] == def_file
    # The resolved SIF path is NOT the existing one — sac built a new one.
    assert resolved != existing_sif


# ---------------------------------------------------------------------------
# Lifecycle (mocked subprocess)
# ---------------------------------------------------------------------------


def test_is_running_false_when_no_pid_file(state_root: Path, tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    assert rt.is_running(cfg) is False


def test_stop_succeeds_when_pid_absent(state_root: Path, tmp_path: Path) -> None:
    """Stopping a never-started agent must not raise."""
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    assert rt.stop(cfg) is True


def test_stop_clears_pid_file_after_kill(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    state_dir = rt._state_dir(cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / APPTAINER_PID_FILE).write_text("12345")

    captured: dict = {}

    def fake_kill(pid: int, sig: int) -> None:
        captured["pid"] = pid
        captured["sig"] = sig

    monkeypatch.setattr(
        "scitex_agent_container.runtimes._apptainer_runtime.os.kill", fake_kill
    )
    assert rt.stop(cfg) is True
    assert captured["pid"] == 12345
    assert not (state_dir / APPTAINER_PID_FILE).is_file()
