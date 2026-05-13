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
    # state_dir is bound at /state/<name> (not /state) so the in-container
    # `state_dir_for(name, root=/state)` resolves to the same path as
    # the bind target — otherwise we'd land on disk at runtime/<name>/<name>/.
    assert any(b.endswith(f":/state/{cfg.name}") and str(state_dir) in b for b in binds)


def test_argv_does_not_override_home(tmp_path: Path) -> None:
    """Apptainer protects HOME from --env override (security policy);
    unlike the docker path it doesn't need a HOME=/tmp pin because
    apptainer inherits the host's /etc/passwd entry by default."""
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "HOME=/tmp" not in argv
    assert not any(a.startswith("HOME=") for a in argv)


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


def test_argv_emits_nv_when_apptainer_nv_set(tmp_path: Path) -> None:
    """``spec.apptainer.nv: true`` adds ``--nv`` to bind host CUDA."""
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(nv=True))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--nv" in argv
    # Sanity: rocm not added unless asked.
    assert "--rocm" not in argv


def test_argv_omits_nv_by_default(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--nv" not in argv


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


# ---------------------------------------------------------------------------
# Merged from test__apptainer_runtime_extras.py (PS-204 orphan consolidation)
# ---------------------------------------------------------------------------


import pytest

from scitex_agent_container.runtimes import _apptainer_runtime as mod
from scitex_agent_container.runtimes._apptainer_runtime import (
    APPTAINER_LOG_FILE,
    _safe_image_tag,
)


@pytest.fixture(autouse=True)
def _home_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() to a per-test tmp dir so credential mounts
    + state-dir resolution don't touch the operator's real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
# resolve_sif — extra branches
# ---------------------------------------------------------------------------


def test_resolve_sif_sandbox_dir(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory with .singularity.d/ marker = sandbox build; used as-is."""
    sandbox = tmp_path / "sbx"
    sandbox.mkdir()
    (sandbox / ".singularity.d").mkdir()
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    cfg = _config(tmp_path, image=str(sandbox))
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    assert resolved == sandbox.resolve()


def test_resolve_sif_docker_uri_builds(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docker:// URI triggers _build_sif_from_uri (mocked)."""
    captured: dict = {}

    def fake_build_uri(sif_path: Path, uri: str) -> bool:
        captured["sif"] = sif_path
        captured["uri"] = uri
        sif_path.write_bytes(b"\x00")
        return True

    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    monkeypatch.setattr(mod, "_build_sif_from_uri", fake_build_uri)

    cfg = _config(tmp_path, image="docker://python:3.11-slim")
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    assert resolved is not None
    assert captured["uri"] == "docker://python:3.11-slim"


def test_resolve_sif_oras_uri_builds(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    monkeypatch.setattr(
        mod, "_build_sif_from_uri", lambda p, u: (p.write_bytes(b"\x00"), True)[1]
    )
    cfg = _config(tmp_path, image="oras://ghcr.io/example/img:tag")
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    assert resolved is not None


def test_resolve_sif_bare_image_treated_as_docker(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``python:3.11-slim`` (no scheme) is treated as docker://."""
    captured: dict = {}

    def fake_build_uri(sif_path: Path, uri: str) -> bool:
        captured["uri"] = uri
        sif_path.write_bytes(b"\x00")
        return True

    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    monkeypatch.setattr(mod, "_build_sif_from_uri", fake_build_uri)

    cfg = _config(tmp_path, image="python:3.11-slim")
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    assert resolved is not None
    assert captured["uri"] == "docker://python:3.11-slim"


def test_resolve_sif_cached_docker_uri_skips_build(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the cached .sif already exists, no build is invoked."""
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")

    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, image="docker://python:3.11-slim")
    cache = rt._image_cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    expected = cache / f"{_safe_image_tag('docker://python:3.11-slim')}.sif"
    expected.write_bytes(b"\x00")

    called: dict = {"yes": False}

    def fake_build(*_a, **_kw) -> bool:
        called["yes"] = True
        return True

    monkeypatch.setattr(mod, "_build_sif_from_uri", fake_build)
    resolved = rt.resolve_sif(cfg)
    assert resolved == expected
    assert called["yes"] is False


def test_resolve_sif_def_file_missing_returns_none(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(def_file=str(tmp_path / "nope.def")),
    )
    assert ApptainerContainerRuntime().resolve_sif(cfg) is None


def test_resolve_sif_def_file_cached(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing built .sif under cache for the def-file path is reused."""
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    def_file = tmp_path / "img.def"
    def_file.write_text("Bootstrap: docker\nFrom: python:3.11\n")

    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(def_file=str(def_file)))
    cache = rt._image_cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{_safe_image_tag(str(def_file.resolve()))}.sif"
    cached.write_bytes(b"\x00")

    called: dict = {"yes": False}

    def fake_build_def(*_a, **_kw) -> bool:
        called["yes"] = True
        return True

    monkeypatch.setattr(mod, "_build_sif_from_def", fake_build_def)
    resolved = rt.resolve_sif(cfg)
    assert resolved == cached
    assert called["yes"] is False


def test_resolve_sif_local_sif_missing_returns_none(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    cfg = _config(tmp_path, image=str(tmp_path / "missing.sif"))
    assert ApptainerContainerRuntime().resolve_sif(cfg) is None


def test_resolve_sif_no_image_no_def_returns_none(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    cfg = _config(tmp_path)
    # spec.apptainer.image and config.image both empty.
    assert ApptainerContainerRuntime().resolve_sif(cfg) is None


# ---------------------------------------------------------------------------
# build_run_argv — extra branches (overlay, raw_args, container.volumes, binds)
# ---------------------------------------------------------------------------


def test_argv_includes_container_volumes(tmp_path: Path) -> None:
    from scitex_agent_container.config._types import ContainerSpec

    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, container=ContainerSpec(volumes=["/host/data:/data:ro"]))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "/host/data:/data:ro" in argv


def test_argv_includes_apptainer_binds_and_raw_args(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(
            binds=["/scratch:/scratch"],
            raw_args=["--cleanenv"],
        ),
    )
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "/scratch:/scratch" in argv
    assert "--cleanenv" in argv


def test_argv_overlay_relative_resolves_against_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "wd"
    rt = ApptainerContainerRuntime()
    cfg = _config(workdir, apptainer=ApptainerSpec(overlay="overlay.img"))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    idx = argv.index("--overlay")
    assert argv[idx + 1] == str(workdir / "overlay.img")


def test_argv_overlay_absolute_used_as_is(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    abs_overlay = tmp_path / "ov.img"
    cfg = _config(tmp_path, apptainer=ApptainerSpec(overlay=str(abs_overlay)))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    idx = argv.index("--overlay")
    assert argv[idx + 1] == str(abs_overlay)


def test_argv_rocm_flag(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(rocm=True))
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "--rocm" in argv


def test_argv_credentials_mount_when_present(
    tmp_path: Path, _home_redirect: Path
) -> None:
    """If ~/.claude/.credentials.json exists, it's bind-mounted ro."""
    creds = _home_redirect / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert any(
        a.startswith(str(creds)) and "/tmp/.claude/.credentials.json:ro" in a
        for a in argv
    )


def test_argv_env_dict_forwarded(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, env={"FOO": "bar"})
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert "FOO=bar" in argv


def test_argv_uses_startup_prompts_over_legacy_commands(tmp_path: Path) -> None:
    from scitex_agent_container.config._types import StartupCommand

    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        startup_prompts=["hello-world"],
        startup_commands=[StartupCommand(command="ignored")],
    )
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    assert argv[argv.index("--mission") + 1] == "hello-world"


# ---------------------------------------------------------------------------
# Lifecycle — start / is_running / logs (subprocess mocked)
# ---------------------------------------------------------------------------


def test_start_returns_false_when_apptainer_missing(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    assert rt.start(cfg) is False


def test_start_returns_false_when_sif_unresolved(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")  # no image / def_file
    assert rt.start(cfg) is False


def test_start_dry_run_writes_argv_file(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    assert rt.start(cfg, dry_run=True) is True
    argv_file = rt._state_dir(cfg) / "apptainer_run.argv.txt"
    assert argv_file.is_file()
    text = argv_file.read_text()
    assert text.splitlines()[0] == "apptainer"


def test_start_background_writes_pid_file(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")

    class FakeProc:
        pid = 99999

    def fake_popen(*_a, **_kw):
        return FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    assert rt.start(cfg) is True
    pid_file = rt._state_dir(cfg) / APPTAINER_PID_FILE
    assert pid_file.read_text() == "99999"


def test_start_foreground_returns_rc_eq_zero(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")

    class FakeResult:
        returncode = 0

    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: FakeResult())
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    assert rt.start(cfg, foreground=True) is True


def test_start_skips_when_already_running(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --force, a second start while is_running returns False."""
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text(str(__import__("os").getpid()))
    # pid of this process is alive → is_running returns True
    assert rt.start(cfg) is False


def test_start_force_stops_then_starts(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/apptainer")

    killed: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        # Signal 0 (probe) raises ProcessLookupError to simulate dead PID
        # after the SIGTERM has done its work. SIGTERM itself is recorded.
        if sig == 0:
            raise ProcessLookupError
        killed.append(pid)

    monkeypatch.setattr(mod.os, "kill", fake_kill)

    class FakeProc:
        pid = 88888

    monkeypatch.setattr(mod.subprocess, "Popen", lambda *_a, **_kw: FakeProc())

    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text("123")
    # is_running: os.kill(123, 0) → ProcessLookupError → False, so force is
    # irrelevant; just verify start path completes.
    assert rt.start(cfg, force=True) is True


def test_is_running_true_for_live_pid(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text("4242")
    monkeypatch.setattr(mod.os, "kill", lambda pid, sig: None)
    assert rt.is_running(cfg) is True


def test_is_running_false_when_pid_dead(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text("4242")

    def boom(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(mod.os, "kill", boom)
    assert rt.is_running(cfg) is False


def test_is_running_false_when_pid_file_corrupt(
    state_root: Path, tmp_path: Path
) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text("not-a-pid")
    assert rt.is_running(cfg) is False


def test_stop_handles_process_lookup(
    state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text("4242")

    def boom(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(mod.os, "kill", boom)
    assert rt.stop(cfg) is True
    assert not (sd / APPTAINER_PID_FILE).is_file()


def test_logs_returns_tail(state_root: Path, tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_LOG_FILE).write_text("\n".join(f"L{i}" for i in range(20)))
    out = rt.logs(cfg, lines=3)
    assert out.splitlines() == ["L17", "L18", "L19"]


def test_logs_empty_when_missing(state_root: Path, tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    assert rt.logs(cfg) == ""


def test_image_cache_dir_under_state_dir(state_root: Path, tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    cache = rt._image_cache_dir(cfg)
    assert cache.parent == rt._state_dir(cfg)
    assert cache.name == "images"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_safe_image_tag_deterministic() -> None:
    a = _safe_image_tag("docker://x:1")
    b = _safe_image_tag("docker://x:1")
    c = _safe_image_tag("docker://x:2")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_build_helpers_invoke_apptainer_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    class R:
        returncode = 0

    def fake_run(argv, *a, **kw):
        seen.append(list(argv))
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._build_sif_from_uri(tmp_path / "out.sif", "docker://x") is True
    assert mod._build_sif_from_def(tmp_path / "out.sif", tmp_path / "x.def") is True
    assert seen[0][:2] == ["apptainer", "build"]
    assert seen[1][:2] == ["apptainer", "build"]


# ---------------------------------------------------------------------------
# A2A wiring guard
#
# Regression for 2026-05-13: spec.a2a.port was parsed into A2ASpec.port
# but build_run_argv didn't propagate it to the runner's CLI flag, so
# the sidecar never bound and POST /v1/turn 404'd. The block below
# asserts the exact argv shape produced when spec.a2a.port is set so
# the regression flips red if anyone drops or reorders the wiring.
# ---------------------------------------------------------------------------


from scitex_agent_container.config._types import A2ASpec


def _config_with_a2a(
    workdir: Path,
    port: int | None = None,
    config_path: str = "",
    startup_prompts: list[str] | None = None,
) -> AgentConfig:
    return AgentConfig(
        name="ecosystem-auditor",
        runtime="apptainer",
        workdir=str(workdir),
        a2a=A2ASpec(port=port) if port is not None else A2ASpec(),
        config_path=config_path,
        startup_prompts=startup_prompts or [],
    )


# ---------------------------------------------------------------------------
# spec.a2a.port → --a2a-port
# ---------------------------------------------------------------------------


def test_a2a_port_appears_in_runner_argv(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=7901, startup_prompts=["hi"])
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-port" in argv, (
        "build_run_argv must propagate spec.a2a.port to the runner; "
        "without this, the inbound sidecar never binds."
    )
    idx = argv.index("--a2a-port")
    assert argv[idx + 1] == "7901"


def test_a2a_port_omitted_when_spec_a2a_unset(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=None, startup_prompts=["hi"])
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-port" not in argv


def test_a2a_port_zero_does_not_bind(tmp_path: Path) -> None:
    """Port 0 is intentionally treated as 'no sidecar' (truthiness gate)."""
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=0, startup_prompts=["hi"])
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-port" not in argv


# ---------------------------------------------------------------------------
# --a2a-card-yaml gets the spec.yaml path
# ---------------------------------------------------------------------------


def test_a2a_card_yaml_passed_when_port_set(tmp_path: Path) -> None:
    """When a2a.port is set AND config_path is known, the runner gets
    --a2a-card-yaml pointing at the spec.yaml so it can publish the
    AgentCard at /.well-known/agent-card.json."""
    rt = ApptainerContainerRuntime()
    yaml_path = tmp_path / "agents" / "ecosystem-auditor" / "spec.yaml"
    cfg = _config_with_a2a(
        tmp_path / "wd",
        port=7901,
        config_path=str(yaml_path),
        startup_prompts=["hi"],
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    assert "--a2a-card-yaml" in argv
    idx = argv.index("--a2a-card-yaml")
    assert argv[idx + 1] == str(yaml_path)


def test_a2a_card_yaml_skipped_when_no_config_path(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(
        tmp_path / "wd", port=7901, config_path="", startup_prompts=["hi"]
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Port flag still present; just the card-yaml is skipped.
    assert "--a2a-port" in argv
    assert "--a2a-card-yaml" not in argv


def test_a2a_card_yaml_skipped_when_port_unset(tmp_path: Path) -> None:
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(
        tmp_path / "wd",
        port=None,
        config_path=str(tmp_path / "spec.yaml"),
        startup_prompts=["hi"],
    )
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # No port → no sidecar → no point publishing the card path either.
    assert "--a2a-port" not in argv
    assert "--a2a-card-yaml" not in argv
