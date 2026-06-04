"""Tests for :class:`ApptainerContainerRuntime` (F-CS18).

No-mocks rewrite: all production hooks are exercised through real
seams.

* ``shutil.which`` is driven by a ``subprocess_shim``-installed fake
  ``apptainer`` binary on ``$PATH`` (and the absence thereof).
* ``subprocess.run`` / ``subprocess.Popen`` execute the same fake
  binary so ``apptainer build`` / ``apptainer exec`` are real OS-level
  process invocations.
* ``Path.home()`` is driven by ``$HOME`` (POSIX semantics) via
  ``env_save_restore``.
* Live-PID assertions launch ``sleep 60`` real subprocesses; dead-PID
  assertions kill a child first so ``os.kill(pid, 0)`` raises a real
  ``ProcessLookupError``.
"""

from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig, ProxySpec
from scitex_agent_container.config._types import (
    A2ASpec,
    ApptainerSpec,
    AutonomousSpec,
    ClaudeSpec,
    ContainerSpec,
)
from scitex_agent_container.runtimes import _apptainer_runtime as mod
from scitex_agent_container.runtimes._apptainer_creds import PinnedAccountError
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    RUNNER_MODULE_AGENT,
    RUNNER_MODULE_PROXY,
)
from scitex_agent_container.runtimes._apptainer_runtime import (
    APPTAINER_LOG_FILE,
    APPTAINER_PID_FILE,
    ApptainerContainerRuntime,
    _safe_image_tag,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home_redirect(tmp_path: Path, env_save_restore) -> Path:
    """Redirect ``$HOME`` to a per-test dir so credentials / state
    resolution don't touch the operator's real home. ``Path.home()`` on
    POSIX reads ``$HOME`` directly — no ``Path.home`` patching needed.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


@pytest.fixture
def state_root(tmp_path: Path, env_save_restore, home_redirect: Path) -> Path:
    """Sandbox the per-agent state-dir root. Sets the env var that
    ``_session_state.DEFAULT_STATE_ROOT`` caches at import time, then
    reloads the module so the next ``state_dir_for`` call sees it."""
    root = tmp_path / "runtime"
    root.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(root))
    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    return root


@pytest.fixture
def apptainer_on_path(subprocess_shim) -> Path:
    """Install a fake ``apptainer`` binary on ``$PATH`` that, when
    invoked as ``apptainer build <out.sif> <src>``, creates the output
    file. Otherwise it's a no-op (exit 0).

    ``shutil.which("apptainer")`` finds this binary by a real PATH
    lookup — no ``which`` monkeypatching.
    """
    bin_dir = subprocess_shim._bin
    script = bin_dir / "apptainer"
    # Argv layout for build: ["build", "<sif>", "<src>"]. The shim
    # *writes the .sif file* so production's `sif_path.is_file()` check
    # passes after the call returns — honest end-to-end behaviour.
    body = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"with open({_q(str(bin_dir / 'apptainer.argv.jsonl'))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == 'build':\n"
        "    Path(sys.argv[2]).write_bytes(b'\\x00')\n"
        "sys.exit(0)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    subprocess_shim._logs["apptainer"] = bin_dir / "apptainer.argv.jsonl"
    return script


def _q(s: str) -> str:
    """Shell-safe Python literal for use in generated scripts."""
    import json

    return json.dumps(s)


@pytest.fixture
def no_apptainer_on_path(env_save_restore, tmp_path: Path) -> Path:
    """Pin ``$PATH`` to an empty directory so ``shutil.which('apptainer')``
    returns ``None`` deterministically — no monkeypatching."""
    empty = tmp_path / "_empty_bin"
    empty.mkdir()
    env_save_restore.set("PATH", str(empty))
    return empty


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _config(workdir: Path, **kw) -> AgentConfig:
    return AgentConfig(
        name=kw.pop("name", "alpha"),
        runtime="apptainer",
        workdir=str(workdir),
        **kw,
    )


def _proxy_config(workdir: Path, **kw) -> AgentConfig:
    return AgentConfig(
        name=kw.pop("name", "proxy-front"),
        runtime="apptainer",
        workdir=str(workdir),
        kind="AgentProxy",
        proxy=ProxySpec(
            upstream=kw.pop("upstream", "https://peer.example.com"),
            trust=kw.pop("trust", "untrusted"),
            redact=list(kw.pop("redact", []) or []),
            timeout_s=kw.pop("timeout_s", 30.0),
        ),
        a2a=A2ASpec(port=kw.pop("a2a_port", None))
        if "a2a_port" in kw or kw.get("a2a_port") is not None
        else A2ASpec(),
        config_path=kw.pop("config_path", ""),
        **kw,
    )


def _extract_inner_argv(argv: list[str]) -> list[str]:
    """Unwrap the D2 ``bash -c "<preflight>\\nexec <inner>"`` wrapper.

    When ``apptainer.relaxed=true`` no wrapper is present and the inner
    argv lives flat in ``argv``.
    """
    for i, a in enumerate(argv):
        if a == "bash" and i + 2 < len(argv) and argv[i + 1] == "-c":
            script = argv[i + 2]
            _, _, exec_line = script.rpartition("\nexec ")
            return shlex.split(exec_line)
        if a == "/usr/bin/tini":
            return argv[i:]
    return []


def _flag_value(argv: list[str], flag: str) -> str:
    idx = argv.index(flag)
    return argv[idx + 1]


def _env_pairs(argv: list[str]) -> dict[str, str]:
    """Decode every ``--env KEY=VAL`` pair in the argv into a dict."""
    out: dict[str, str] = {}
    for i, a in enumerate(argv):
        if a == "--env" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# build_run_argv — top-level shape (pure function, no I/O)
# ---------------------------------------------------------------------------


def test_argv_first_two_tokens_are_apptainer_exec(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert argv[0:2] == ["apptainer", "exec"]


def test_argv_emits_workdir_bind_mount(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    workdir = tmp_path / "wd"
    cfg = _config(workdir)
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    bind_idxs = [i for i, a in enumerate(argv) if a == "--bind"]
    binds = [argv[i + 1] for i in bind_idxs]
    assert any(b.endswith(":/work") and str(workdir) in b for b in binds)


def test_argv_emits_state_dir_bind_mount(tmp_path: Path) -> None:
    # Arrange — state_dir is bound at /state/<name>, not /state.
    rt = ApptainerContainerRuntime()
    state_dir = tmp_path / "state"
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(cfg, state_dir=state_dir, sif_path=tmp_path / "x.sif")
    # Assert
    bind_idxs = [i for i, a in enumerate(argv) if a == "--bind"]
    binds = [argv[i + 1] for i in bind_idxs]
    assert any(b.endswith(f":/state/{cfg.name}") and str(state_dir) in b for b in binds)


def test_argv_does_not_emit_home_tmp_literal(tmp_path: Path) -> None:
    # Arrange — apptainer rejects --env HOME=... so production must
    # never emit it.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "HOME=/tmp" not in argv


def test_argv_does_not_emit_any_home_env(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert not any(a.startswith("HOME=") for a in argv)


def test_argv_does_not_emit_user_flag(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--user" not in argv


def test_argv_binds_overlay_upper_home_over_home_tmpfs(tmp_path: Path) -> None:
    # Arrange — relaxed + directory-overlay + explicit --home. The
    # raw-arg --home /home/agent mounts a fresh tmpfs that would shadow
    # the to_home tree materialised into <overlay>/upper/home/agent. The
    # runtime must bind that upper-home over the container HOME so
    # $HOME/.mcp.json (per-agent MCP delivery) survives the tmpfs.
    rt = ApptainerContainerRuntime()
    overlay_dir = tmp_path / "overlay"
    upper_home = overlay_dir / "upper" / "home" / "agent"
    upper_home.mkdir(parents=True, exist_ok=True)  # simulate deploy_to_home_overlay
    cfg = _config(tmp_path / "wd")
    cfg.apptainer = ApptainerSpec(
        relaxed=True,
        raw_args=[
            "--containall",
            "--home",
            "/home/agent",
            "--overlay",
            str(overlay_dir),
        ],
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert — the upper-home bind exists, targeting the container HOME.
    bind_idxs = [i for i, a in enumerate(argv) if a == "--bind"]
    binds = [argv[i + 1] for i in bind_idxs]
    assert f"{upper_home}:/home/agent" in binds


def test_argv_overlay_home_bind_appended_after_home_raw_arg(tmp_path: Path) -> None:
    # Arrange — order matters: the bind must come AFTER the raw-arg
    # --home so apptainer applies it over the home tmpfs (verified via
    # `mount`: a late --bind wins over the --home tmpfs).
    rt = ApptainerContainerRuntime()
    overlay_dir = tmp_path / "overlay"
    upper_home = overlay_dir / "upper" / "home" / "agent"
    upper_home.mkdir(parents=True, exist_ok=True)
    cfg = _config(tmp_path / "wd")
    cfg.apptainer = ApptainerSpec(
        relaxed=True,
        raw_args=[
            "--containall",
            "--home",
            "/home/agent",
            "--overlay",
            str(overlay_dir),
        ],
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    home_idx = argv.index("--home")
    upper_bind = f"{upper_home}:/home/agent"
    bind_value_idx = argv.index(upper_bind)
    assert bind_value_idx > home_idx


def test_argv_no_overlay_home_bind_without_overlay(tmp_path: Path) -> None:
    # Arrange — a relaxed spec WITHOUT an overlay must not get the
    # upper-home bind (resolver returns None → no-op).
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    cfg.apptainer = ApptainerSpec(
        relaxed=True, raw_args=["--containall", "--home", "/home/agent"]
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert — no bind whose target is /home/agent originating from an overlay upper.
    bind_idxs = [i for i, a in enumerate(argv) if a == "--bind"]
    binds = [argv[i + 1] for i in bind_idxs]
    assert not any("/overlay/upper/home/agent:/home/agent" in b for b in binds)


def test_argv_runs_runner_module_via_tini(tmp_path: Path) -> None:
    # Arrange — use startup_prompts (claude mission). startup_commands
    # now wraps the inner argv in bash -lc, so the runner is no longer
    # at argv[0] of the inner — see test__apptainer_inner_argv.py for
    # the shell-wrapping behavior.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, startup_prompts=["say hi"])
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    inner = _extract_inner_argv(argv)
    # Assert
    assert inner[0] == "/usr/bin/tini"


def test_argv_inner_invokes_claude_session_module(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, startup_prompts=["say hi"])
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    )
    # Assert
    assert "scitex_agent_container._runners.claude_session" in inner


def test_argv_inner_carries_mission_from_startup_prompt(tmp_path: Path) -> None:
    # Arrange — startup_prompts is the mission source (no fallback
    # from startup_commands after 2026-05-17 refactor).
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, startup_prompts=["say hi"])
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    )
    # Assert
    assert inner[inner.index("--mission") + 1] == "say hi"


def test_argv_forwards_sac_anthropic_api_key_env(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("SAC_ANTHROPIC_API_KEY", "sk-ant-api-test")
    env_save_restore.delete("ANTHROPIC_API_KEY")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "SAC_ANTHROPIC_API_KEY=sk-ant-api-test" in argv


def test_argv_emits_nv_flag_when_apptainer_nv_true(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(nv=True))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--nv" in argv


def test_argv_omits_rocm_when_only_nv_requested(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(nv=True))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--rocm" not in argv


def test_argv_omits_nv_flag_by_default(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--nv" not in argv


def test_argv_forwards_autonomous_enabled_flag(tmp_path: Path) -> None:
    # Arrange — use startup_prompts so the inner argv is not bash-wrapped.
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        startup_prompts=["seed"],
        autonomous=AutonomousSpec(enabled=True, drive_until="OK", max_turns=7),
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    )
    # Assert
    assert "--autonomous-enabled" in inner


def test_argv_forwards_autonomous_drive_until_value(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        startup_prompts=["seed"],
        autonomous=AutonomousSpec(enabled=True, drive_until="OK", max_turns=7),
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    )
    # Assert
    assert inner[inner.index("--autonomous-drive-until") + 1] == "OK"


def test_argv_forwards_autonomous_max_turns_value(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        startup_prompts=["seed"],
        autonomous=AutonomousSpec(enabled=True, drive_until="OK", max_turns=7),
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    )
    # Assert
    assert inner[inner.index("--autonomous-max-turns") + 1] == "7"


# ---------------------------------------------------------------------------
# resolve_sif — real apptainer-on-PATH seam
# ---------------------------------------------------------------------------


def test_resolve_sif_uses_existing_local_sif_path(
    tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange — an existing .sif on disk is used directly.
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    cfg = _config(tmp_path, image=str(sif))
    # Act
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert resolved == sif


def test_resolve_sif_returns_none_when_apptainer_missing(
    tmp_path: Path, no_apptainer_on_path: Path
) -> None:
    # Arrange
    cfg = _config(tmp_path, image=str(tmp_path / "x.sif"))
    # Act
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert resolved is None


def test_resolve_sif_def_file_takes_precedence_over_image(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path
) -> None:
    # Arrange — def_file is set: production must build from def, not
    # reuse spec.image.
    existing_sif = tmp_path / "existing.sif"
    existing_sif.write_bytes(b"\x00")
    def_file = tmp_path / "extend.def"
    def_file.write_text("Bootstrap: docker\nFrom: python:3.11-slim\n")
    cfg = _config(
        tmp_path,
        image=str(existing_sif),
        apptainer=ApptainerSpec(def_file=str(def_file)),
    )
    # Act
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert — resolved path is NOT the existing sif (built fresh from def).
    assert resolved != existing_sif


def test_resolve_sif_def_file_passes_def_path_to_apptainer_build(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange
    def_file = tmp_path / "extend.def"
    def_file.write_text("Bootstrap: docker\nFrom: python:3.11-slim\n")
    cfg = _config(tmp_path, apptainer=ApptainerSpec(def_file=str(def_file)))
    # Act
    ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert subprocess_shim.argv_for("apptainer")[-1] == str(def_file.resolve())


def test_resolve_sif_def_file_uses_build_subcommand(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange
    def_file = tmp_path / "extend.def"
    def_file.write_text("Bootstrap: docker\nFrom: python:3.11-slim\n")
    cfg = _config(tmp_path, apptainer=ApptainerSpec(def_file=str(def_file)))
    # Act
    ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert subprocess_shim.argv_for("apptainer")[0] == "build"


def test_resolve_sif_sandbox_dir_used_as_is(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path
) -> None:
    # Arrange — `.singularity.d/` marks a sandbox build dir.
    sandbox = tmp_path / "sbx"
    sandbox.mkdir()
    (sandbox / ".singularity.d").mkdir()
    cfg = _config(tmp_path, image=str(sandbox))
    # Act
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert resolved == sandbox.resolve()


def test_resolve_sif_docker_uri_invokes_apptainer_build_with_uri(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange
    cfg = _config(tmp_path, image="docker://python:3.11-slim")
    # Act
    ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert subprocess_shim.argv_for("apptainer")[-1] == "docker://python:3.11-slim"


def test_resolve_sif_oras_uri_invokes_apptainer_build_with_uri(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange
    cfg = _config(tmp_path, image="oras://ghcr.io/example/img:tag")
    # Act
    ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert subprocess_shim.argv_for("apptainer")[-1] == "oras://ghcr.io/example/img:tag"


def test_resolve_sif_bare_image_passes_docker_scheme_to_build(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange — `python:3.11-slim` (no scheme) gets `docker://` prepended.
    cfg = _config(tmp_path, image="python:3.11-slim")
    # Act
    ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert subprocess_shim.argv_for("apptainer")[-1] == "docker://python:3.11-slim"


def test_resolve_sif_cached_docker_uri_returns_cached_path(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange — pre-populate the cache so production short-circuits.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, image="docker://python:3.11-slim")
    cache = rt._image_cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    expected = cache / f"{_safe_image_tag('docker://python:3.11-slim')}.sif"
    expected.write_bytes(b"\x00")
    # Act
    resolved = rt.resolve_sif(cfg)
    # Assert
    assert resolved == expected


def test_resolve_sif_cached_docker_uri_does_not_invoke_build(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, image="docker://python:3.11-slim")
    cache = rt._image_cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    expected = cache / f"{_safe_image_tag('docker://python:3.11-slim')}.sif"
    expected.write_bytes(b"\x00")
    # Act
    rt.resolve_sif(cfg)
    # Assert
    assert subprocess_shim.call_count("apptainer") == 0


def test_resolve_sif_returns_none_for_missing_def_file(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path
) -> None:
    # Arrange
    cfg = _config(
        tmp_path, apptainer=ApptainerSpec(def_file=str(tmp_path / "nope.def"))
    )
    # Act
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert resolved is None


def test_resolve_sif_def_file_returns_cached_sif_path(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange — cached SIF for def_file path is reused.
    def_file = tmp_path / "img.def"
    def_file.write_text("Bootstrap: docker\nFrom: python:3.11\n")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(def_file=str(def_file)))
    cache = rt._image_cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{_safe_image_tag(str(def_file.resolve()))}.sif"
    cached.write_bytes(b"\x00")
    # Act
    resolved = rt.resolve_sif(cfg)
    # Assert
    assert resolved == cached


def test_resolve_sif_def_file_cache_hit_skips_build(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path, subprocess_shim
) -> None:
    # Arrange
    def_file = tmp_path / "img.def"
    def_file.write_text("Bootstrap: docker\nFrom: python:3.11\n")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(def_file=str(def_file)))
    cache = rt._image_cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / f"{_safe_image_tag(str(def_file.resolve()))}.sif"
    cached.write_bytes(b"\x00")
    # Act
    rt.resolve_sif(cfg)
    # Assert
    assert subprocess_shim.call_count("apptainer") == 0


def test_resolve_sif_returns_none_when_local_sif_missing(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path
) -> None:
    # Arrange
    cfg = _config(tmp_path, image=str(tmp_path / "missing.sif"))
    # Act
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert resolved is None


def test_resolve_sif_returns_none_when_no_image_or_def(
    tmp_path: Path, apptainer_on_path: Path, state_root: Path
) -> None:
    # Arrange
    cfg = _config(tmp_path)
    # Act
    resolved = ApptainerContainerRuntime().resolve_sif(cfg)
    # Assert
    assert resolved is None


# ---------------------------------------------------------------------------
# build_run_argv — extra branches
# ---------------------------------------------------------------------------


def test_argv_includes_container_volumes_flag(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, container=ContainerSpec(volumes=["/host/data:/data:ro"]))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "/host/data:/data:ro" in argv


def test_argv_includes_apptainer_extra_binds(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(binds=["/scratch:/scratch"]))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "/scratch:/scratch" in argv


def test_argv_appends_apptainer_raw_args(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(raw_args=["--cleanenv"]))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--cleanenv" in argv


def test_argv_overlay_relative_resolves_against_workdir(tmp_path: Path) -> None:
    # Arrange — overlay file must exist on disk (auto-create skipped
    # when overlay_size is unset, but the existence check still passes
    # because we pre-create the file).
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "overlay.img").write_bytes(b"")
    rt = ApptainerContainerRuntime()
    cfg = _config(workdir, apptainer=ApptainerSpec(overlay="overlay.img"))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv[argv.index("--overlay") + 1] == str(workdir / "overlay.img")


def test_argv_overlay_absolute_used_unchanged(tmp_path: Path) -> None:
    # Arrange — same as above, pre-create the overlay so existence
    # check passes without exercising auto-create.
    rt = ApptainerContainerRuntime()
    abs_overlay = tmp_path / "ov.img"
    abs_overlay.write_bytes(b"")
    cfg = _config(tmp_path, apptainer=ApptainerSpec(overlay=str(abs_overlay)))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv[argv.index("--overlay") + 1] == str(abs_overlay)


# ---------------------------------------------------------------------------
# Declarative overlay auto-create (overlay_size + overlay_create_if_missing)
#
# We use the subprocess_shim — a real fake-binary on $PATH — rather than
# rewriting production's subprocess.run. The shim records every call and
# (for `overlay create`) materialises the output file so production's
# existence checks stay honest.
# ---------------------------------------------------------------------------


@pytest.fixture
def apptainer_overlay_shim(subprocess_shim) -> Path:
    """Install a fake ``apptainer`` on ``$PATH`` that, when invoked as
    ``apptainer overlay create --size <MB> <path>``, materialises the
    target file (zero bytes). All argvs are recorded in
    ``subprocess_shim`` so tests can read them back. Non-overlay
    subcommands also record but are no-ops.

    This replaces ``monkeypatch.setattr(subprocess, "run", ...)`` —
    production calls the real ``subprocess.run`` which finds the fake
    on PATH.
    """
    bin_dir = subprocess_shim._bin
    script = bin_dir / "apptainer"
    body = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"with open({_q(str(bin_dir / 'apptainer.argv.jsonl'))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        # overlay create --size <MB> <path>: touch the path so the next
        # existence check passes.
        "if len(sys.argv) >= 6 and sys.argv[1:5] == ['overlay', 'create', '--size'] + [sys.argv[3]]:\n"
        "    Path(sys.argv[5]).write_bytes(b'')\n"
        "sys.exit(0)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    subprocess_shim._logs["apptainer"] = bin_dir / "apptainer.argv.jsonl"
    return script


@pytest.fixture
def apptainer_overlay_shim_fail(subprocess_shim) -> Path:
    """Like ``apptainer_overlay_shim`` but exits 1 with ``out of disk``
    on stderr — drives the RuntimeError path."""
    bin_dir = subprocess_shim._bin
    script = bin_dir / "apptainer"
    body = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({_q(str(bin_dir / 'apptainer.argv.jsonl'))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stderr.write('out of disk')\n"
        "sys.exit(1)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    subprocess_shim._logs["apptainer"] = bin_dir / "apptainer.argv.jsonl"
    return script


def test_overlay_auto_create_when_missing_invokes_apptainer_overlay_create(
    tmp_path: Path, apptainer_overlay_shim: Path, subprocess_shim
) -> None:
    # Arrange — overlay file does not exist; overlay_size is set; the
    # default overlay_create_if_missing=True triggers _create_overlay_image.
    overlay = tmp_path / "ov.img"
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(overlay=str(overlay), overlay_size="100M"),
    )
    # Act
    rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert — the shim recorded exactly one apptainer overlay create call.
    assert subprocess_shim.invocations("apptainer") == [
        ["overlay", "create", "--size", "100", str(overlay)]
    ]


def test_overlay_auto_create_emits_overlay_flag_in_argv(
    tmp_path: Path, apptainer_overlay_shim: Path
) -> None:
    # Arrange — same setup; verify the post-create argv carries --overlay.
    overlay = tmp_path / "ov.img"
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(overlay=str(overlay), overlay_size="100M"),
    )
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv[argv.index("--overlay") + 1] == str(overlay)


def test_overlay_missing_without_size_raises_filenotfound(tmp_path: Path) -> None:
    # Arrange — overlay missing, no overlay_size → must fail loudly.
    rt = ApptainerContainerRuntime()
    overlay = tmp_path / "missing.img"
    cfg = _config(tmp_path, apptainer=ApptainerSpec(overlay=str(overlay)))
    # Act
    # Assert
    with pytest.raises(FileNotFoundError, match="overlay_size"):
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")


def test_overlay_create_disabled_raises_filenotfound(
    tmp_path: Path, apptainer_overlay_shim: Path
) -> None:
    # Arrange — overlay missing, overlay_size set, but
    # overlay_create_if_missing=False → must raise (different message).
    overlay = tmp_path / "no-create.img"
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(
            overlay=str(overlay),
            overlay_size="5G",
            overlay_create_if_missing=False,
        ),
    )
    # Act
    # Assert
    with pytest.raises(FileNotFoundError, match="overlay_create_if_missing=false"):
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")


@pytest.fixture
def _disabled_create_run(
    tmp_path: Path, apptainer_overlay_shim: Path, subprocess_shim
) -> int:
    """Invoke build_run_argv with overlay missing + create disabled,
    swallow the FileNotFoundError (verified by sibling test), and yield
    the post-call apptainer call count so the assertion test can pin
    exactly one fact."""
    overlay = tmp_path / "no-create.img"
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(
            overlay=str(overlay),
            overlay_size="5G",
            overlay_create_if_missing=False,
        ),
    )
    try:
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    except FileNotFoundError:
        pass
    return subprocess_shim.call_count("apptainer")


def test_overlay_create_disabled_does_not_invoke_apptainer(
    _disabled_create_run: int,
) -> None:
    # Arrange
    call_count = _disabled_create_run
    # Act
    # Assert
    assert call_count == 0


def test_overlay_exists_does_not_invoke_apptainer(
    tmp_path: Path, apptainer_overlay_shim: Path, subprocess_shim
) -> None:
    # Arrange — overlay file already on disk → no subprocess call.
    overlay = tmp_path / "ov.img"
    overlay.write_bytes(b"")
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(overlay=str(overlay), overlay_size="5G"),
    )
    # Act
    rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert subprocess_shim.call_count("apptainer") == 0


def test_overlay_exists_still_emits_overlay_flag(
    tmp_path: Path, apptainer_overlay_shim: Path
) -> None:
    # Arrange — verify --overlay still lands in argv when file exists.
    overlay = tmp_path / "ov.img"
    overlay.write_bytes(b"")
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(overlay=str(overlay), overlay_size="5G"),
    )
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv[argv.index("--overlay") + 1] == str(overlay)


# ---------------------------------------------------------------------------
# _create_overlay_image — size parsing + subprocess result handling
# ---------------------------------------------------------------------------


def test_overlay_size_5G_parses_to_5120_megabytes(
    tmp_path: Path, apptainer_overlay_shim: Path, subprocess_shim
) -> None:
    # Arrange
    path = tmp_path / "ov.img"
    # Act
    mod._create_overlay_image(path, "5G")
    # Assert — 5G → 5120 MB.
    assert subprocess_shim.argv_for("apptainer") == [
        "overlay",
        "create",
        "--size",
        "5120",
        str(path),
    ]


def test_overlay_size_500M_parses_to_500_megabytes(
    tmp_path: Path, apptainer_overlay_shim: Path, subprocess_shim
) -> None:
    # Arrange
    path = tmp_path / "ov.img"
    # Act
    mod._create_overlay_image(path, "500M")
    # Assert
    assert subprocess_shim.argv_for("apptainer") == [
        "overlay",
        "create",
        "--size",
        "500",
        str(path),
    ]


def test_overlay_size_unparseable_raises_valueerror(tmp_path: Path) -> None:
    # Arrange — unparseable size; no shim required because production
    # must reject before invoking apptainer.
    path = tmp_path / "ov.img"
    # Act
    # Assert
    with pytest.raises(ValueError, match="unparseable"):
        mod._create_overlay_image(path, "abc")


def test_overlay_size_kilobytes_rejected_as_valueerror(tmp_path: Path) -> None:
    # Arrange — K/KB units are unsupported (apptainer --size takes int MB).
    path = tmp_path / "ov.img"
    # Act
    # Assert
    with pytest.raises(ValueError, match="unparseable"):
        mod._create_overlay_image(path, "100K")


def test_overlay_subprocess_failure_raises_runtimeerror(
    tmp_path: Path, apptainer_overlay_shim_fail: Path
) -> None:
    # Arrange — fail-shim exits 1 with 'out of disk' on stderr.
    path = tmp_path / "ov.img"
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="out of disk"):
        mod._create_overlay_image(path, "5G")


def test_argv_emits_rocm_flag_when_requested(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(rocm=True))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--rocm" in argv


def test_argv_mounts_credentials_dir_when_present(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — Path.home() reads $HOME, redirected by the fixture.
    # Post task #13 the unpinned branch dir-binds ``~/.claude/`` at
    # ``/tmp/sac-claude`` (same shape as the pinned branch). The legacy
    # single-file bind to ``/tmp/sac-claude/.credentials.json`` is
    # retired — atomic-replace refresh would unlink the bound inode.
    creds = home_redirect / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert — bind source is the credentials file's PARENT (~/.claude/).
    assert any(a == f"{creds.parent}:/tmp/sac-claude:rw" for a in argv)


def test_argv_credentials_bind_is_read_write(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — RW lets the in-container CLI refresh the OAuth
    # accessToken in place when it expires (~1h cadence), avoiding the
    # manual scp-from-lead dance to re-seed expired peers.
    creds = home_redirect / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    creds_arg = next(a for a in argv if ":/tmp/sac-claude:" in a)
    # Assert
    assert ":ro" not in creds_arg


def test_argv_sets_claude_config_dir_when_credentials_present(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange
    creds = home_redirect / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "CLAUDE_CONFIG_DIR=/tmp/sac-claude" in argv


def test_argv_forwards_arbitrary_env_dict(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, env={"FOO": "bar"})
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "FOO=bar" in argv


def test_argv_startup_prompts_populates_mission(tmp_path: Path) -> None:
    # Arrange — after 2026-05-17 refactor, startup_commands and
    # startup_prompts go to two different destinations with no
    # fallback between them. This test verifies startup_prompts
    # populates --mission. The shell-exec behavior of
    # startup_commands lives in test__apptainer_inner_argv.py.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, startup_prompts=["hello-world"])
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    )
    # Assert
    assert inner[inner.index("--mission") + 1] == "hello-world"


# ---------------------------------------------------------------------------
# Per-agent OAuth account pin (spec.claude.account) — frozen boot-copy
# ---------------------------------------------------------------------------


def _save_snapshot(home: Path, name: str, body: str | None = None) -> Path:
    """Write a real saved-account snapshot under the home's account store
    and return its ``.credentials.json`` path.

    Mirrors the on-disk layout
    ``~/.scitex/agent-container/accounts/<name>/.credentials.json`` that
    ``sac account save`` produces. The default body carries a VALID
    (far-future) ``claudeAiOauth.expiresAt`` so the fail-loud pin
    resolver (``_apptainer_creds.resolve_cred_file``) accepts it; tests
    that need a stale/odd snapshot pass an explicit ``body``.
    """
    if body is None:
        far_future_ms = int((time.time() + 86_400) * 1_000)
        body = '{"claudeAiOauth": {"expiresAt": %d}}' % far_future_ms
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / name
    acct_dir.mkdir(parents=True, exist_ok=True)
    snap = acct_dir / ".credentials.json"
    snap.write_text(body)
    return snap


def test_argv_pins_account_binds_snapshot_directory_not_host_file(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — host live file AND a saved snapshot both exist; the pin
    # must bind the snapshot's per-account DIRECTORY (task #11 — the
    # prior single-file bind orphaned its inode under the host's
    # atomic-replace writers in creds_sync / account_store /
    # claude_usage, regressing into the per-copy collision-401 disease
    # the snapshot model was meant to fix). The bound dir's child
    # ``.credentials.json`` remains the snapshot itself; refresh
    # writeback by the in-container CLI lands in the same on-disk file
    # every same-account agent reads from.
    host_creds = home_redirect / ".claude" / ".credentials.json"
    host_creds.parent.mkdir(parents=True, exist_ok=True)
    host_creds.write_text('{"host": true}')
    snap = _save_snapshot(home_redirect, "alpha")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, claude=ClaudeSpec(account="alpha"))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=state_dir, sif_path=tmp_path / "x.sif")
    creds_arg = next(
        a
        for a in argv
        if a.startswith(str(snap.parent) + ":") and a.endswith(":/tmp/sac-claude:rw")
    )
    # Assert — the bound host-side source IS the account directory
    # (snapshot.parent), neither the snapshot file alone, a per-agent
    # copy, nor the host live file.
    assert creds_arg.split(":")[0] == str(snap.parent)


def test_argv_pins_account_does_not_create_state_dir_copy(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — snapshot has distinctive bytes (plus a VALID future
    # expiresAt). The runtime MUST NOT write a state-dir copy — the
    # bind target is the snapshot itself (operator task #15).
    far_future_ms = int((time.time() + 86_400) * 1_000)
    body = '{"pinned": "beta-bytes", "claudeAiOauth": {"expiresAt": %d}}' % (
        far_future_ms
    )
    _save_snapshot(home_redirect, "beta", body=body)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, claude=ClaudeSpec(account="beta"))
    # Act
    rt.build_run_argv(cfg, state_dir=state_dir, sif_path=tmp_path / "x.sif")
    legacy_copy = state_dir / "claude" / ".credentials.json"
    # Assert — no per-agent copy materialised; the snapshot is the
    # only place the in-container CLI's :rw refresh writeback lands.
    assert not legacy_copy.exists()


def test_argv_no_account_dir_binds_host_claude(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — account="" (unpinned default) now dir-binds ``~/.claude/``
    # at ``/tmp/sac-claude`` (post task #13, same shape as the pinned
    # branch). The legacy single-file bind is retired — atomic-replace
    # refreshes orphaned the bound inode → //deleted → 401 at expiry.
    host_creds = home_redirect / ".claude" / ".credentials.json"
    host_creds.parent.mkdir(parents=True, exist_ok=True)
    host_creds.write_text("{}")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, claude=ClaudeSpec(account=""))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert — bind source is the credentials file's PARENT (~/.claude/);
    # bind dest is the DIRECTORY /tmp/sac-claude, not the file inside it.
    assert any(a == f"{host_creds.parent}:/tmp/sac-claude:rw" for a in argv)


def test_argv_pinned_account_missing_snapshot_raises_pinned_account_error(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — pin names an account with NO snapshot; host file exists.
    # A pinned agent must NEVER silently fall back to the host live file
    # (a different account); it must hard-error with the remedy hint.
    host_creds = home_redirect / ".claude" / ".credentials.json"
    host_creds.parent.mkdir(parents=True, exist_ok=True)
    host_creds.write_text("{}")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, claude=ClaudeSpec(account="ghost"))
    # Act
    ctx = pytest.raises(PinnedAccountError)
    # Assert — fail loud, never bind the host file.
    with ctx:
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")


def test_argv_pinned_account_expired_snapshot_raises_pinned_account_error(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — snapshot exists but its OAuth token already expired; a
    # pinned agent must refuse to launch with a stale token.
    past_ms = int((time.time() - 86_400) * 1_000)
    _save_snapshot(
        home_redirect,
        "stale",
        body='{"claudeAiOauth": {"expiresAt": %d}}' % past_ms,
    )
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, claude=ClaudeSpec(account="stale"))
    # Act
    ctx = pytest.raises(PinnedAccountError)
    # Assert
    with ctx:
        rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")


# ---------------------------------------------------------------------------
# Lifecycle — start / stop / is_running / logs (real subprocesses)
# ---------------------------------------------------------------------------


def test_start_raises_explanatory_runtime_error_when_apptainer_binary_missing(
    state_root: Path, tmp_path: Path, no_apptainer_on_path: Path
) -> None:
    # Arrange — clew handoff 2026-05-31 P1: the legacy silent
    # ``return False`` here surfaced upstream as a generic ``Failed
    # to start agent ...`` with no diagnostic; the runtime now names
    # the missing binary AND the nested-SIF cause explicitly.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")

    # Act
    def _call():
        return rt.start(cfg)

    # Assert
    with pytest.raises(RuntimeError, match=r"apptainer binary not found"):
        _call()


def test_start_error_message_names_nested_apptainer_escape_hint(
    state_root: Path, tmp_path: Path, no_apptainer_on_path: Path
) -> None:
    # Arrange — the nested-SIF cause is the common path on Spartan
    # compute (agent running inside a SIF that doesn't bundle
    # apptainer on PATH). The error message must surface the
    # ``spec.apptainer.nested_mode: "escape"`` lever the operator
    # needs, not just "apptainer missing".
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")

    # Act
    captured: list[BaseException] = []
    try:
        rt.start(cfg)
    except RuntimeError as exc:
        captured.append(exc)

    # Assert
    assert (
        len(captured) == 1
        and "nested_mode" in str(captured[0])
        and "escape" in str(captured[0])
    )


def test_start_error_message_names_the_agent_being_started(
    state_root: Path, tmp_path: Path, no_apptainer_on_path: Path
) -> None:
    # Arrange — the operator's terminal may have many concurrent
    # ``sac agents start`` runs in flight; naming the agent in the
    # error tells them which spec.yaml to look at.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", name="zeta-bm175")

    # Act
    captured: list[BaseException] = []
    try:
        rt.start(cfg)
    except RuntimeError as exc:
        captured.append(exc)

    # Assert
    assert len(captured) == 1 and "zeta-bm175" in str(captured[0])


def test_start_dry_run_does_not_raise_when_apptainer_binary_missing(
    state_root: Path, tmp_path: Path, no_apptainer_on_path: Path
) -> None:
    # Arrange — dry-run only emits argv to a state-dir file and
    # never calls ``apptainer exec``. A dev box / CI runner without
    # apptainer installed must still be able to validate the
    # ``sac agents start --dry-run`` argv path; the loud raise added
    # for the no-apptainer case must skip when dry_run=True. The
    # spec needs an image so the inner ``resolve_sif`` returns a
    # value (dry-run hits the same code path otherwise).
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))

    # Act
    ok = rt.start(cfg, dry_run=True)

    # Assert
    assert ok is True


def test_start_returns_false_when_sif_cannot_be_resolved(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange — apptainer on PATH but no image/def_file configured.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    started = rt.start(cfg)
    # Assert
    assert started is False


def test_start_dry_run_returns_true(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    # Act
    ok = rt.start(cfg, dry_run=True)
    # Assert
    assert ok is True


def test_start_dry_run_writes_argv_text_file(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    # Act
    rt.start(cfg, dry_run=True)
    # Assert
    assert (rt._state_dir(cfg) / "apptainer_run.argv.txt").is_file()


def test_start_dry_run_argv_file_begins_with_apptainer(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    # Act
    rt.start(cfg, dry_run=True)
    # Assert
    argv_file = rt._state_dir(cfg) / "apptainer_run.argv.txt"
    assert argv_file.read_text().splitlines()[0] == "apptainer"


def test_start_background_returns_true(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    # Act
    ok = rt.start(cfg)
    # Assert
    assert ok is True


def test_start_background_writes_positive_pid_to_pid_file(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange — the fake apptainer exits 0 immediately, but the PID
    # file is written before the child exits because Popen returns
    # right after fork+exec.
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    # Act
    rt.start(cfg)
    # Assert
    pid = int((rt._state_dir(cfg) / APPTAINER_PID_FILE).read_text())
    assert pid > 0


def test_start_foreground_returns_true_on_rc_zero(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange — fake apptainer exits 0.
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    # Act
    ok = rt.start(cfg, foreground=True)
    # Assert
    assert ok is True


def test_start_skips_when_already_running(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange — write our own PID; it's alive (this process), so
    # is_running returns True.
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text(str(os.getpid()))
    # Act
    ok = rt.start(cfg)
    # Assert
    assert ok is False


def test_start_force_overrides_stale_pid_and_starts(
    state_root: Path, tmp_path: Path, apptainer_on_path: Path
) -> None:
    # Arrange — record a long-running child PID, then call start with
    # force=True. The child is later cleaned up after stop.
    sif = tmp_path / "ready.sif"
    sif.write_bytes(b"\x00")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", image=str(sif))
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["sleep", "60"])
    try:
        (sd / APPTAINER_PID_FILE).write_text(str(proc.pid))
        # Act
        ok = rt.start(cfg, force=True)
    finally:
        try:
            os.kill(proc.pid, 15)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    # Assert
    assert ok is True


def test_is_running_false_when_no_pid_file(state_root: Path, tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    running = rt.is_running(cfg)
    # Assert
    assert running is False


def test_is_running_true_for_live_pid(state_root: Path, tmp_path: Path) -> None:
    # Arrange — spawn a real sleep process and record its PID.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["sleep", "60"])
    try:
        (sd / APPTAINER_PID_FILE).write_text(str(proc.pid))
        # Act
        running = rt.is_running(cfg)
    finally:
        try:
            os.kill(proc.pid, 15)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    # Assert
    assert running is True


def test_is_running_false_for_dead_pid(state_root: Path, tmp_path: Path) -> None:
    # Arrange — start a process and reap it so os.kill(pid, 0) raises
    # ProcessLookupError naturally.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    (sd / APPTAINER_PID_FILE).write_text(str(proc.pid))
    # Act
    running = rt.is_running(cfg)
    # Assert
    assert running is False


def test_is_running_false_when_pid_file_corrupt(
    state_root: Path, tmp_path: Path
) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_PID_FILE).write_text("not-a-pid")
    # Act
    running = rt.is_running(cfg)
    # Assert
    assert running is False


def test_stop_succeeds_when_no_pid_file(state_root: Path, tmp_path: Path) -> None:
    # Arrange — stopping a never-started agent must be a no-op success.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    stopped = rt.stop(cfg)
    # Assert
    assert stopped is True


def test_stop_returns_true_after_killing_live_pid(
    state_root: Path, tmp_path: Path
) -> None:
    # Arrange — real sleep child; stop() will SIGTERM it for real.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["sleep", "60"])
    (sd / APPTAINER_PID_FILE).write_text(str(proc.pid))
    try:
        # Act
        ok = rt.stop(cfg)
    finally:
        try:
            os.kill(proc.pid, 9)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    # Assert
    assert ok is True


def test_stop_removes_pid_file_after_killing_live_pid(
    state_root: Path, tmp_path: Path
) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["sleep", "60"])
    (sd / APPTAINER_PID_FILE).write_text(str(proc.pid))
    try:
        # Act
        rt.stop(cfg)
    finally:
        try:
            os.kill(proc.pid, 9)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
    # Assert
    assert not (sd / APPTAINER_PID_FILE).is_file()


def test_stop_returns_true_for_already_dead_pid(
    state_root: Path, tmp_path: Path
) -> None:
    # Arrange — pid points at a reaped child; SIGTERM raises
    # ProcessLookupError, production swallows it.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    (sd / APPTAINER_PID_FILE).write_text(str(proc.pid))
    # Act
    ok = rt.stop(cfg)
    # Assert
    assert ok is True


def test_stop_removes_pid_file_for_already_dead_pid(
    state_root: Path, tmp_path: Path
) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["true"])
    proc.wait(timeout=5)
    (sd / APPTAINER_PID_FILE).write_text(str(proc.pid))
    # Act
    rt.stop(cfg)
    # Assert
    assert not (sd / APPTAINER_PID_FILE).is_file()


def test_logs_returns_last_n_lines(state_root: Path, tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    sd = rt._state_dir(cfg)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / APPTAINER_LOG_FILE).write_text("\n".join(f"L{i}" for i in range(20)))
    # Act
    out = rt.logs(cfg, lines=3)
    # Assert
    assert out.splitlines() == ["L17", "L18", "L19"]


def test_logs_empty_string_when_log_missing(state_root: Path, tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    out = rt.logs(cfg)
    # Assert
    assert out == ""


def test_image_cache_dir_lives_under_state_dir(
    state_root: Path, tmp_path: Path
) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    cache = rt._image_cache_dir(cfg)
    # Assert
    assert cache.parent == rt._state_dir(cfg)


def test_image_cache_dir_is_named_images(state_root: Path, tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    cache = rt._image_cache_dir(cfg)
    # Assert
    assert cache.name == "images"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_safe_image_tag_is_deterministic_for_same_input() -> None:
    # Arrange
    reference = "docker://x:1"
    # Act
    a = _safe_image_tag(reference)
    b = _safe_image_tag(reference)
    # Assert
    assert a == b


def test_safe_image_tag_differs_across_inputs() -> None:
    # Arrange
    ref_a = "docker://x:1"
    ref_b = "docker://x:2"
    # Act
    a = _safe_image_tag(ref_a)
    c = _safe_image_tag(ref_b)
    # Assert
    assert a != c


def test_safe_image_tag_has_sixteen_char_length() -> None:
    # Arrange
    reference = "docker://x:1"
    # Act
    a = _safe_image_tag(reference)
    # Assert
    assert len(a) == 16


def test_build_sif_from_uri_returns_true_on_success(
    tmp_path: Path, apptainer_on_path: Path, subprocess_shim
) -> None:
    # Arrange
    out = tmp_path / "out.sif"
    # Act
    ok = mod._build_sif_from_uri(out, "docker://x")
    # Assert
    assert ok is True


def test_build_sif_from_uri_invokes_apptainer_build_subcommand(
    tmp_path: Path, apptainer_on_path: Path, subprocess_shim
) -> None:
    # Arrange
    out = tmp_path / "out.sif"
    # Act
    mod._build_sif_from_uri(out, "docker://x")
    # Assert
    assert subprocess_shim.argv_for("apptainer")[:1] == ["build"]


def test_build_sif_from_def_returns_true_on_success(
    tmp_path: Path, apptainer_on_path: Path, subprocess_shim
) -> None:
    # Arrange
    def_file = tmp_path / "x.def"
    def_file.write_text("Bootstrap: docker\n")
    # Act
    ok = mod._build_sif_from_def(tmp_path / "out.sif", def_file)
    # Assert
    assert ok is True


def test_build_sif_from_def_invokes_apptainer_build_subcommand(
    tmp_path: Path, apptainer_on_path: Path, subprocess_shim
) -> None:
    # Arrange
    def_file = tmp_path / "x.def"
    def_file.write_text("Bootstrap: docker\n")
    # Act
    mod._build_sif_from_def(tmp_path / "out.sif", def_file)
    # Assert
    assert subprocess_shim.argv_for("apptainer")[:1] == ["build"]


# ---------------------------------------------------------------------------
# Fail-loud regression (backlog #4) — _build_sif_from_* MUST surface
# the apptainer stderr on non-zero rc rather than swallowing it into a
# bare ``False`` return. The pre-fix shape ate the diagnostic and the
# operator saw only a generic "Failed to start agent" upstream.
# ---------------------------------------------------------------------------


def test_build_sif_from_uri_raises_runtime_error_on_apptainer_failure(
    tmp_path: Path, subprocess_shim
) -> None:
    # Arrange — install a fake apptainer that fails with a distinctive
    # stderr the test asserts on (proves the stderr is forwarded, not
    # just that the call raises).
    subprocess_shim.install(
        "apptainer", exit=1, stderr="OCI pull failed: image not found"
    )
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(RuntimeError, match="OCI pull failed: image not found"):
        mod._build_sif_from_uri(tmp_path / "out.sif", "docker://nope")


def test_build_sif_from_def_raises_runtime_error_on_apptainer_failure(
    tmp_path: Path, subprocess_shim
) -> None:
    # Arrange
    def_file = tmp_path / "x.def"
    def_file.write_text("Bootstrap: docker\n")
    subprocess_shim.install(
        "apptainer", exit=1, stderr="def parse error: missing Bootstrap"
    )
    # Act
    # Assert — pytest.raises is the assertion (TQ007: one per test).
    with pytest.raises(RuntimeError, match="def parse error: missing Bootstrap"):
        mod._build_sif_from_def(tmp_path / "out.sif", def_file)


# ---------------------------------------------------------------------------
# A2A wiring guard — spec.a2a.port → --a2a-port, plus card-yaml path
# ---------------------------------------------------------------------------


def _config_with_a2a(
    workdir: Path,
    *,
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


def test_a2a_port_propagates_into_runner_argv(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=7901, startup_prompts=["hi"])
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert _flag_value(inner, "--a2a-port") == "7901"


def test_a2a_port_omitted_when_spec_a2a_unset(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=None, startup_prompts=["hi"])
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert "--a2a-port" not in inner


def test_a2a_port_zero_does_not_bind_sidecar(tmp_path: Path) -> None:
    # Arrange — port 0 is intentionally falsy → no sidecar.
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(tmp_path / "wd", port=0, startup_prompts=["hi"])
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert "--a2a-port" not in inner


def test_a2a_card_yaml_passed_when_port_and_config_path_set(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    yaml_path = tmp_path / "agents" / "ecosystem-auditor" / "spec.yaml"
    cfg = _config_with_a2a(
        tmp_path / "wd", port=7901, config_path=str(yaml_path), startup_prompts=["hi"]
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert _flag_value(inner, "--a2a-card-yaml") == str(yaml_path)


def test_a2a_card_yaml_skipped_when_config_path_missing(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(
        tmp_path / "wd", port=7901, config_path="", startup_prompts=["hi"]
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert "--a2a-card-yaml" not in inner


def test_a2a_card_yaml_skipped_when_port_unset(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config_with_a2a(
        tmp_path / "wd",
        port=None,
        config_path=str(tmp_path / "spec.yaml"),
        startup_prompts=["hi"],
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert "--a2a-card-yaml" not in inner


# ---------------------------------------------------------------------------
# AgentProxy dispatch
# ---------------------------------------------------------------------------


def test_kind_agent_proxy_dispatches_to_a2a_proxy_runner(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd")
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert RUNNER_MODULE_PROXY in inner


def test_kind_agent_proxy_excludes_sdk_runner_module(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd")
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert RUNNER_MODULE_AGENT not in inner


def test_kind_agent_default_still_uses_claude_session(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="agent-front", runtime="apptainer", workdir=str(tmp_path / "wd")
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert RUNNER_MODULE_AGENT in inner


def test_kind_agent_default_excludes_proxy_runner_module(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="agent-front", runtime="apptainer", workdir=str(tmp_path / "wd")
    )
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert RUNNER_MODULE_PROXY not in inner


# ---------------------------------------------------------------------------
# Proxy argv shape — required spec.proxy.* fields
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy_inner_argv(tmp_path: Path) -> list[str]:
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(
        tmp_path / "wd",
        upstream="https://peer.example.com",
        trust="local-mesh",
        redact=["SECRET", "TOKEN"],
        timeout_s=12.5,
    )
    return _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )


@pytest.mark.parametrize(
    "flag,expected",
    [
        ("--upstream", "https://peer.example.com"),
        ("--trust", "local-mesh"),
        ("--redact", "SECRET,TOKEN"),
        ("--timeout-s", "12.5"),
        ("--name", "proxy-front"),
        ("--state-root", "/state"),
    ],
)
def test_proxy_argv_propagates_spec_proxy_field(
    proxy_inner_argv: list[str], flag: str, expected: str
) -> None:
    # Arrange
    argv = proxy_inner_argv
    # Act
    actual = _flag_value(argv, flag)
    # Assert
    assert actual == expected


def test_proxy_empty_redact_serialises_to_empty_string(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd", redact=[])
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert _flag_value(inner, "--redact") == ""


def test_proxy_a2a_port_propagates_when_set(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd", a2a_port=7902)
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert _flag_value(inner, "--a2a-port") == "7902"


def test_proxy_a2a_card_yaml_set_when_port_and_path_provided(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    yaml_path = tmp_path / "agents" / "proxy-front" / "spec.yaml"
    cfg = _proxy_config(tmp_path / "wd", a2a_port=7902, config_path=str(yaml_path))
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert _flag_value(inner, "--a2a-card-yaml") == str(yaml_path)


def test_proxy_a2a_port_omitted_when_unset(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd", a2a_port=None)
    # Act
    inner = _extract_inner_argv(
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    )
    # Assert
    assert "--a2a-port" not in inner


@pytest.mark.parametrize(
    "forbidden",
    ["--mission", "--autonomous-enabled", "--autonomous-drive-until", "--print-stream"],
)
def test_proxy_argv_excludes_sdk_only_flag(tmp_path: Path, forbidden: str) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _proxy_config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert forbidden not in argv


# ---------------------------------------------------------------------------
# SAC_LISTEN_BASE_URL env injection
# ---------------------------------------------------------------------------


def test_default_listen_url_injected_when_no_config(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BASE_URL") == "http://127.0.0.1:7878"


def test_config_listen_port_propagates_to_env(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("listen:\n  port: 9090\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_yaml))
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BASE_URL") == "http://127.0.0.1:9090"


def test_config_listen_host_propagates_to_env(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("listen:\n  host: 100.64.1.2\n  port: 7878\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_yaml))
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path)
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BASE_URL") == "http://100.64.1.2:7878"


# ---------------------------------------------------------------------------
# Bus-auth bearer injection (FIX 1) — SAC_LISTEN_BEARER must be injected
# into EVERY apptainer spec (including relaxed:true), read from the host
# token file the listen server writes. Missing token → BASE_URL only +
# loud warning, no crash, no bearer.
# ---------------------------------------------------------------------------


def _write_listen_token(home: Path, token: str) -> Path:
    """Materialize the canonical listen token file under a redirected HOME.

    Resolves the path exactly as production does (``default_token_path``),
    so the test exercises the real resolver rather than a hard-coded path.
    """
    from scitex_agent_container._listen.tokens import default_token_path

    tok_path = default_token_path(home=home)
    tok_path.parent.mkdir(parents=True, exist_ok=True)
    tok_path.write_text(token, encoding="utf-8")
    return tok_path


def test_argv_injects_listen_bearer_from_token_file(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — standard spec with a real token file under HOME.
    _write_listen_token(home_redirect, "tok-abc123")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BEARER") == "tok-abc123"


def test_argv_still_injects_base_url_alongside_bearer(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange
    _write_listen_token(home_redirect, "tok-abc123")
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BASE_URL") == "http://127.0.0.1:7878"


def test_relaxed_spec_omits_preflight_wrapper(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — confirm relaxed mode is actually in effect.
    _write_listen_token(home_redirect, "tok-relaxed-xyz")
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="relaxed-agent",
        runtime="apptainer",
        workdir=str(tmp_path / "wd"),
        apptainer=ApptainerSpec(relaxed=True, raw_args=["--userns", "--cleanenv"]),
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "--containall" not in argv


def test_relaxed_spec_still_injects_listen_bearer(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — bus-auth injection must be unconditional w.r.t relaxed.
    _write_listen_token(home_redirect, "tok-relaxed-xyz")
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="relaxed-agent",
        runtime="apptainer",
        workdir=str(tmp_path / "wd"),
        apptainer=ApptainerSpec(relaxed=True, raw_args=["--userns", "--cleanenv"]),
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BEARER") == "tok-relaxed-xyz"


def test_missing_token_omits_bearer(tmp_path: Path, home_redirect: Path) -> None:
    # Arrange — no token file under HOME.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "SAC_LISTEN_BEARER" not in _env_pairs(argv)


def test_missing_token_still_injects_base_url(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — no token file under HOME.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BASE_URL") == "http://127.0.0.1:7878"


def test_missing_token_logs_loud_warning(
    tmp_path: Path, home_redirect: Path, caplog
) -> None:
    # Arrange — no token file → loud warning, no silent fallback.
    import logging

    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    with caplog.at_level(logging.WARNING):
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )
    # Assert
    assert any(
        "SAC_LISTEN_BEARER not injected" in rec.getMessage() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Fail-loud: server:sac channel + unresolvable bearer
# ---------------------------------------------------------------------------
# When the channel adapter is actually registered (spec.claude.channels has
# server:sac) but the bus bearer can't be resolved, the runtime must REFUSE
# to launch rather than start an agent whose adapter can never subscribe
# (delivered_subscriber_count would always be 0). This is the live blocker.


def _relaxed_bus_cfg(workdir: Path) -> AgentConfig:
    """Production proj-scitex-agent-container shape: relaxed + explicit
    raw_args carrying NO SAC_LISTEN_*, plus channels=[server:sac]."""
    return AgentConfig(
        name="relaxed-bus-agent",
        runtime="apptainer",
        workdir=str(workdir),
        apptainer=ApptainerSpec(relaxed=True, raw_args=["--userns", "--containall"]),
        claude=ClaudeSpec(channels=["server:sac"]),
    )


def test_relaxed_with_server_sac_channel_injects_bearer(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange
    _write_listen_token(home_redirect, "tok-relaxed-bus")
    rt = ApptainerContainerRuntime()
    cfg = _relaxed_bus_cfg(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BEARER") == "tok-relaxed-bus"


def test_relaxed_with_server_sac_channel_injects_base_url(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange
    _write_listen_token(home_redirect, "tok-relaxed-bus")
    rt = ApptainerContainerRuntime()
    cfg = _relaxed_bus_cfg(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _env_pairs(argv).get("SAC_LISTEN_BASE_URL") == "http://127.0.0.1:7878"


def test_server_sac_channel_missing_token_fails_loud(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _relaxed_bus_cfg(tmp_path / "wd")
    # Act
    # Assert — refuse to launch (no silent degradation).
    with pytest.raises(RuntimeError, match="server:sac"):
        rt.build_run_argv(
            cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
        )


def test_no_channel_missing_token_does_not_raise(
    tmp_path: Path, home_redirect: Path
) -> None:
    # Arrange — no server:sac channel → a missing token is harmless because
    # nothing subscribes; the runtime warns but must NOT raise.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "SAC_LISTEN_BEARER" not in _env_pairs(argv)


# ---------------------------------------------------------------------------
# Hardened isolation defaults (--containall, --cleanenv, --writable-tmpfs)
# ---------------------------------------------------------------------------


def test_argv_includes_containall_by_default(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="x", runtime="apptainer", workdir=str(tmp_path), apptainer=ApptainerSpec()
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "--containall" in argv


def test_argv_containall_precedes_sif_path(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    cfg = AgentConfig(
        name="x", runtime="apptainer", workdir=str(tmp_path), apptainer=ApptainerSpec()
    )
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path / "state", sif_path=sif)
    # Assert
    assert argv.index("--containall") < argv.index(str(sif))


def test_argv_omits_containall_when_relaxed_true(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="x",
        runtime="apptainer",
        workdir=str(tmp_path),
        apptainer=ApptainerSpec(relaxed=True),
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "--containall" not in argv


def test_argv_does_not_double_containall_when_operator_declares(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = AgentConfig(
        name="x",
        runtime="apptainer",
        workdir=str(tmp_path),
        apptainer=ApptainerSpec(raw_args=["--containall", "--cleanenv"]),
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert argv.count("--containall") == 1


def test_cleanenv_present_by_default(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec())
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--cleanenv" in argv


def test_cleanenv_absent_when_relaxed_true(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(relaxed=True))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--cleanenv" not in argv


def test_cleanenv_not_doubled_when_operator_set(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(raw_args=["--cleanenv"]))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv.count("--cleanenv") == 1


def test_writable_tmpfs_present_by_default(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec())
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--writable-tmpfs" in argv


def test_writable_tmpfs_absent_when_overlay_configured(tmp_path: Path) -> None:
    # Arrange — apptainer rejects --writable-tmpfs + --overlay together.
    rt = ApptainerContainerRuntime()
    overlay = tmp_path / "ov.img"
    overlay.write_bytes(b"")
    cfg = _config(tmp_path, apptainer=ApptainerSpec(overlay=str(overlay)))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--writable-tmpfs" not in argv


def test_overlay_still_emitted_alongside_no_writable_tmpfs(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    overlay = tmp_path / "ov.img"
    overlay.write_bytes(b"")
    cfg = _config(tmp_path, apptainer=ApptainerSpec(overlay=str(overlay)))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--overlay" in argv


def test_writable_tmpfs_absent_when_relaxed(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(relaxed=True))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--writable-tmpfs" not in argv


def test_writable_tmpfs_not_doubled_when_operator_set(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(raw_args=["--writable-tmpfs"]))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv.count("--writable-tmpfs") == 1


@pytest.mark.parametrize("flag", ["--containall", "--cleanenv", "--writable-tmpfs"])
def test_all_three_hardening_flags_coexist_by_default(
    tmp_path: Path, flag: str
) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec())
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert flag in argv


# ---------------------------------------------------------------------------
# Canonical --home /home/agent + apptainer.fakeroot opt-in
# ---------------------------------------------------------------------------


def test_home_canonical_default_is_home_agent(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec())
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv[argv.index("--home") + 1] == "/home/agent"


def test_home_flag_absent_when_relaxed(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(relaxed=True))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--home" not in argv


def test_home_flag_not_doubled_when_operator_set(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path, apptainer=ApptainerSpec(raw_args=["--home", "/custom/home"])
    )
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv.count("--home") == 1


def test_home_operator_value_preserved(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path, apptainer=ApptainerSpec(raw_args=["--home", "/custom/home"])
    )
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv[argv.index("--home") + 1] == "/custom/home"


def test_fakeroot_appended_when_spec_opts_in(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec(fakeroot=True))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--fakeroot" in argv


def test_fakeroot_absent_by_default(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path, apptainer=ApptainerSpec())
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert "--fakeroot" not in argv


def test_fakeroot_not_doubled_when_operator_also_sets(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path,
        apptainer=ApptainerSpec(fakeroot=True, raw_args=["--fakeroot"]),
    )
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=tmp_path / "x.sif")
    # Assert
    assert argv.count("--fakeroot") == 1


# ---------------------------------------------------------------------------
# /tmp scratch sizing — spec.apptainer.tmpfs_size (default "2G")
# ---------------------------------------------------------------------------
#
# A --containall apptainer container otherwise gets a 64 MB session tmpfs
# at /tmp, which fills mid-run during the full test suite. sac emits
# --workdir <state_dir>/tmp-scratch to relocate /tmp onto the host
# filesystem. See runtimes/_apptainer_tmpfs.py.


def test_tmpfs_default_emits_workdir_flag(tmp_path: Path) -> None:
    # Arrange — bare apptainer spec → dataclass default tmpfs_size "2G".
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", apptainer=ApptainerSpec())
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "--workdir" in argv


def test_tmpfs_default_workdir_points_at_state_scratch(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    state_dir = tmp_path / "state"
    cfg = _config(tmp_path / "wd", apptainer=ApptainerSpec())
    # Act
    argv = rt.build_run_argv(cfg, state_dir=state_dir, sif_path=tmp_path / "x.sif")
    # Assert
    assert _flag_value(argv, "--workdir") == str(state_dir / "tmp-scratch")


def test_tmpfs_default_applies_without_apptainer_block(tmp_path: Path) -> None:
    # Arrange — no apptainer block at all still gets the larger /tmp.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "--workdir" in argv


def test_tmpfs_override_size_still_emits_workdir(tmp_path: Path) -> None:
    # Arrange — a roomy override that the tmp_path filesystem satisfies.
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", apptainer=ApptainerSpec(tmpfs_size="512M"))
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _flag_value(argv, "--workdir") == str(tmp_path / "state" / "tmp-scratch")


def test_tmpfs_empty_opts_out_of_workdir(tmp_path: Path) -> None:
    # Arrange — explicit "" means "use the legacy 64 MB session tmpfs".
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd", apptainer=ApptainerSpec(tmpfs_size=""))
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert "--workdir" not in argv


def test_tmpfs_not_doubled_when_operator_sets_workdir(tmp_path: Path) -> None:
    # Arrange — operator's own --workdir in raw_args wins; sac skips its.
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path / "wd",
        apptainer=ApptainerSpec(raw_args=["--workdir", "/scratch/mine"]),
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert argv.count("--workdir") == 1


def test_tmpfs_operator_workdir_value_preserved(tmp_path: Path) -> None:
    # Arrange
    rt = ApptainerContainerRuntime()
    cfg = _config(
        tmp_path / "wd",
        apptainer=ApptainerSpec(raw_args=["--workdir", "/scratch/mine"]),
    )
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert _flag_value(argv, "--workdir") == "/scratch/mine"


# ---------------------------------------------------------------------------
# #16 — quota-cache.json bind-in + telegrammer env
#
# Every agent SIF gets read-only visibility on the host's quota-cache.json
# (refreshed every 10 min by host cron). Bind is conditional on the host
# file existing — quota-cron-less hosts (CI, fresh installs) must still
# be able to launch agents.
# ---------------------------------------------------------------------------


def _binds(argv: list[str]) -> list[str]:
    """Return every value following a ``--bind`` flag."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]


def test_argv_binds_quota_cache_when_host_file_exists(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — point the runtime at a fixture file that DOES exist.
    # Honest no-mocks redirect: SAC_QUOTA_CACHE_HOST_PATH is a real env
    # override resolved at call time by ``_resolve_quota_cache_host_path``.
    fake_host_cache = tmp_path / "quota-cache.json"
    fake_host_cache.write_text("{}", encoding="utf-8")
    env_save_restore.set("SAC_QUOTA_CACHE_HOST_PATH", str(fake_host_cache))
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert — bind shape: ``<host>:<container>:ro``, container path is
    # the module constant (PR-A and `sac account quota` default to it).
    expected = f"{fake_host_cache}:{mod.QUOTA_CACHE_CONTAINER_PATH}:ro"
    assert expected in _binds(argv)


def test_argv_omits_quota_cache_bind_when_host_file_absent(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — host path points nowhere (apptainer would otherwise
    # fail-hard on the missing bind source — the runtime must skip it).
    env_save_restore.set(
        "SAC_QUOTA_CACHE_HOST_PATH", str(tmp_path / "definitely-not-there.json")
    )
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    # Assert
    assert not any(mod.QUOTA_CACHE_CONTAINER_PATH in b for b in _binds(argv))


def test_argv_exposes_quota_cache_path_env_when_bound(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — when the bind is added, the runtime must also point the
    # in-container telegrammer at the bound container path (its default
    # is the host path, which is not visible inside the SIF).
    fake_host_cache = tmp_path / "quota-cache.json"
    fake_host_cache.write_text("{}", encoding="utf-8")
    env_save_restore.set("SAC_QUOTA_CACHE_HOST_PATH", str(fake_host_cache))
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    env = _env_pairs(argv)
    # Assert
    assert (
        env.get("CLAUDE_CODE_TELEGRAMMER_TELEGRAM_QUOTA_CACHE_PATH")
        == mod.QUOTA_CACHE_CONTAINER_PATH
    )


def test_argv_omits_quota_cache_path_env_when_unbound(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("SAC_QUOTA_CACHE_HOST_PATH", str(tmp_path / "missing.json"))
    rt = ApptainerContainerRuntime()
    cfg = _config(tmp_path / "wd")
    # Act
    argv = rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )
    env = _env_pairs(argv)
    # Assert
    assert "CLAUDE_CODE_TELEGRAMMER_TELEGRAM_QUOTA_CACHE_PATH" not in env


def test_quota_cache_container_path_default_is_var_sac(tmp_path: Path) -> None:
    # Arrange — the constant is the single source of truth shared by the
    # apptainer runtime (bind dst), the telegrammer (env-pointed reader
    # default), and `sac account quota` (default cache path). A rename
    # without coordinating the three sites silently breaks #16.
    constant = mod.QUOTA_CACHE_CONTAINER_PATH
    # Act
    actual = str(constant)
    # Assert
    assert actual == "/var/sac/quota-cache.json"
