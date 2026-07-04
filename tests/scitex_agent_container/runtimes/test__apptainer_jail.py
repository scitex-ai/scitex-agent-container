"""Tests for the jailed-capsule mount-boundary assert
(:mod:`runtimes._apptainer_jail`).

Security guardrail: a jailed capsule (``solver`` group auto-on, or
``spec.apptainer.jail=true``) must NEVER mount a shared / heavy-metadata
filesystem. The launch command-builder FORCES ``--containall`` and
REFUSES (fail-loud, before exec) any bind whose realpath-resolved source
lands under a forbidden prefix.

No mocks / no ``monkeypatch`` — real ``AgentConfig`` specs, real
``build_run_argv`` argv assembly, real on-disk symlinks (``tmp_path``) to
prove realpath resolution, and a real ``os.environ`` set/restore fixture
for the env-injection cases. Assertions are on the built argv / the
raised error, never a live apptainer run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes import _apptainer_jail as jail
from scitex_agent_container.runtimes._apptainer_jail import (
    ENV_BIND_VARS,
    enforce_jail,
    is_jailed,
    scrub_bind_env,
)
from scitex_agent_container.runtimes._apptainer_runtime import (
    ApptainerContainerRuntime,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_bind_env():
    """Clear ambient apptainer bind / prefix-override env vars per test.

    sac may run INSIDE a container that already exports ``APPTAINER_BIND``
    (e.g. its own ``~/.scitex/todo`` bind) — real ``os.environ`` state
    that would otherwise leak into ``enforce_jail`` and make these tests
    non-deterministic. Snapshot + pop before each test, restore after.
    No mocks: real environment mutation with real teardown.
    """
    keys = list(ENV_BIND_VARS) + [jail.FORBIDDEN_PREFIXES_ENV]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    yield
    for k, original in saved.items():
        if original is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = original


@pytest.fixture
def env_set() -> Callable[[str, str], None]:
    """Set real ``os.environ`` keys, restoring originals on teardown.

    A no-mocks replacement for ``monkeypatch.setenv`` — production reads
    the real environment.
    """
    saved: dict[str, str | None] = {}

    def _set(key: str, value: str) -> None:
        if key not in saved:
            saved[key] = os.environ.get(key)
        os.environ[key] = value

    yield _set

    for key, original in saved.items():
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def _solver_cfg(workdir, **kw) -> AgentConfig:
    """A jailed-by-group config (metadata.labels.groups: [solver])."""
    return AgentConfig(
        name=kw.pop("name", "solver-x"),
        runtime="apptainer",
        workdir=str(workdir),
        labels=kw.pop("labels", {"groups": ["solver"]}),
        **kw,
    )


def _build(cfg: AgentConfig, tmp_path: Path) -> list[str]:
    rt = ApptainerContainerRuntime()
    return rt.build_run_argv(
        cfg, state_dir=tmp_path / "state", sif_path=tmp_path / "x.sif"
    )


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------
def test_solver_group_is_jailed(tmp_path: Path) -> None:
    # Arrange
    cfg = _solver_cfg(tmp_path / "wd")
    # Act
    jailed = is_jailed(cfg)
    # Assert
    assert jailed is True


def test_jail_flag_is_jailed(tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(
        name="j",
        runtime="apptainer",
        workdir=str(tmp_path),
        apptainer=ApptainerSpec(jail=True),
    )
    # Act
    jailed = is_jailed(cfg)
    # Assert
    assert jailed is True


def test_non_solver_non_jail_is_not_jailed(tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(
        name="dev",
        runtime="apptainer",
        workdir=str(tmp_path),
        labels={"groups": ["developer"]},
    )
    # Act
    jailed = is_jailed(cfg)
    # Assert
    assert jailed is False


# ---------------------------------------------------------------------------
# (a) jailed spec with a raw_args --bind /data/gpfs → fail-loud
# ---------------------------------------------------------------------------
def test_jailed_raw_args_gpfs_bind_refused(tmp_path: Path) -> None:
    # Arrange
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(raw_args=["--bind", "/data/gpfs/proj:/x"]),
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=re.escape("/data/gpfs/proj")):
        _build(cfg, tmp_path)


def test_jailed_spec_binds_gpfs_scratch_refused(tmp_path: Path) -> None:
    # Arrange
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(binds=["/data/scratch/data:/data:ro"]),
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=re.escape("/data/scratch/data")):
        _build(cfg, tmp_path)


# ---------------------------------------------------------------------------
# (b) symlinked source resolving under /data/gpfs → rejected (realpath)
# ---------------------------------------------------------------------------
def test_jailed_symlinked_source_realpath_refused(tmp_path: Path) -> None:
    # Arrange — a benign-looking source under tmp_path that is actually a
    # symlink to /data/gpfs. Only realpath (not a textual check) catches
    # it; the link target need not exist (realpath resolves the string).
    link = tmp_path / "cache"
    link.symlink_to("/data/gpfs")
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(binds=[f"{link}/sub:/x"]),
    )
    # Act
    # Assert — the message names the realpath, not the benign link.
    with pytest.raises(RuntimeError, match=re.escape("/data/gpfs/sub")):
        _build(cfg, tmp_path)


# ---------------------------------------------------------------------------
# (c) /homework near-miss → NOT rejected (component-aware match)
# ---------------------------------------------------------------------------
def test_jailed_homework_near_miss_allowed(tmp_path: Path) -> None:
    # Arrange — /homework must NOT match the /home forbidden prefix.
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(binds=["/homework/mine:/x"]),
    )
    # Act
    argv = _build(cfg, tmp_path)
    # Assert
    assert "--containall" in argv


# ---------------------------------------------------------------------------
# (d) clean jailed spec (node-local bind) → launches, --containall present
# ---------------------------------------------------------------------------
def test_jailed_node_local_bind_launches_with_containall(tmp_path: Path) -> None:
    # Arrange
    node_local = tmp_path / "nl"
    node_local.mkdir()
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(binds=[f"{node_local}:/work:rw"]),
    )
    # Act
    argv = _build(cfg, tmp_path)
    # Assert
    assert "--containall" in argv


def test_jailed_relaxed_still_forces_containall(tmp_path: Path) -> None:
    # Arrange — relaxed=true normally SKIPS the hardened --containall; the
    # jail assert FORCES it back regardless (non-bypassable).
    node_local = tmp_path / "nl"
    node_local.mkdir()
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(relaxed=True, binds=[f"{node_local}:/work:rw"]),
    )
    # Act
    argv = _build(cfg, tmp_path)
    # Assert
    assert "--containall" in argv


# ---------------------------------------------------------------------------
# (e) --pwd under /home → rejected
# ---------------------------------------------------------------------------
def test_jailed_pwd_under_home_refused(tmp_path: Path) -> None:
    # Arrange
    cfg = _solver_cfg("/home/someuser/proj")
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=re.escape("/home/someuser/proj")):
        _build(cfg, tmp_path)


# ---------------------------------------------------------------------------
# (f) non-jailed non-solver agent with a /data/gpfs bind → NOT rejected
# ---------------------------------------------------------------------------
def test_non_jailed_gpfs_bind_unaffected(tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(
        name="full",
        runtime="apptainer",
        workdir=str(tmp_path / "wd"),
        labels={"groups": ["developer"]},
        apptainer=ApptainerSpec(binds=["/data/gpfs/proj:/data:ro"]),
    )
    # Act
    argv = _build(cfg, tmp_path)
    # Assert
    assert "/data/gpfs/proj:/data:ro" in argv


# ---------------------------------------------------------------------------
# (g) missing-leaf node-local source → launches (realpath tolerates it)
# ---------------------------------------------------------------------------
def test_jailed_missing_leaf_source_launches(tmp_path: Path) -> None:
    # Arrange — $TMPDIR/workdir is created AT launch, so the leaf is absent
    # when the assert runs. realpath resolves the existing parent + appends
    # the missing leaf WITHOUT failing.
    existing_parent = tmp_path / "tmpdir"
    existing_parent.mkdir()
    missing = existing_parent / "workdir"  # deliberately not created
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(binds=[f"{missing}:/work:rw"]),
    )
    # Act
    argv = _build(cfg, tmp_path)
    # Assert
    assert "--containall" in argv


def test_jailed_missing_leaf_over_gpfs_symlink_still_refused(tmp_path: Path) -> None:
    # Arrange — a missing leaf whose EXISTING parent is a symlink to
    # /data/gpfs must still be caught (realpath resolves the parent).
    link = tmp_path / "cache"
    link.symlink_to("/data/gpfs")
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(binds=[f"{link}/not-yet-created:/x"]),
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=re.escape("/data/gpfs/not-yet-created")):
        _build(cfg, tmp_path)


# ---------------------------------------------------------------------------
# (h) env-injected bind vars → rejected fail-loud + scrubbed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("var", list(ENV_BIND_VARS))
def test_jailed_env_injected_gpfs_bind_refused(
    tmp_path: Path, env_set: Callable[[str, str], None], var: str
) -> None:
    # Arrange
    env_set(var, "/data/gpfs/x:/x")
    cfg = _solver_cfg(tmp_path / "wd")
    # Act
    # Assert — the message names both the env var and the offending path.
    with pytest.raises(RuntimeError, match=re.escape(var) + r".*" + re.escape("/data/gpfs/x")):
        _build(cfg, tmp_path)


def test_scrub_bind_env_removes_bind_vars() -> None:
    # Arrange
    env = {v: "/data/gpfs/x:/x" for v in ENV_BIND_VARS}
    # Act
    scrub_bind_env(env)
    # Assert
    assert not any(v in env for v in ENV_BIND_VARS)


def test_scrub_bind_env_keeps_unrelated_vars() -> None:
    # Arrange
    env = {"KEEP": "1", "APPTAINER_BIND": "/data/gpfs/x:/x"}
    # Act
    scrub_bind_env(env)
    # Assert
    assert env == {"KEEP": "1"}


def test_non_jailed_env_bind_unaffected(
    tmp_path: Path, env_set: Callable[[str, str], None]
) -> None:
    # Arrange
    env_set("APPTAINER_BIND", "/data/gpfs/x:/x")
    cfg = AgentConfig(
        name="full",
        runtime="apptainer",
        workdir=str(tmp_path / "wd"),
        labels={"groups": ["developer"]},
    )
    # Act
    argv = _build(cfg, tmp_path)
    # Assert — non-jailed builds normally (no raise); argv is well-formed.
    assert argv[0:2] == ["apptainer", "exec"]


# ---------------------------------------------------------------------------
# Configurable forbidden-prefix override (component-aware, realpath)
# ---------------------------------------------------------------------------
def test_forbidden_prefix_env_override(
    tmp_path: Path, env_set: Callable[[str, str], None]
) -> None:
    # Arrange — point the forbidden set at a real tmp dir; a symlink
    # resolving there is rejected, proving the override + realpath on an
    # EXISTING target.
    fake_shared = tmp_path / "shared_fs"
    fake_shared.mkdir()
    env_set(jail.FORBIDDEN_PREFIXES_ENV, str(fake_shared))
    link = tmp_path / "innocent"
    link.symlink_to(fake_shared)
    cfg = _solver_cfg(
        tmp_path / "wd",
        apptainer=ApptainerSpec(binds=[f"{link}/data:/x"]),
    )
    # Act
    # Assert
    with pytest.raises(RuntimeError, match=re.escape(str(fake_shared))):
        _build(cfg, tmp_path)


# ---------------------------------------------------------------------------
# enforce_jail unit surface (env passed explicitly)
# ---------------------------------------------------------------------------
def test_enforce_jail_noop_for_non_jailed() -> None:
    # Arrange
    cfg = AgentConfig(name="d", runtime="apptainer", workdir="/tmp/wd")
    argv = ["apptainer", "exec", "--bind", "/data/gpfs/x:/x", "img.sif", "cmd"]
    # Act
    enforce_jail(cfg, argv, env={})
    # Assert — not jailed → no --containall injected, no raise.
    assert "--containall" not in argv


def test_enforce_jail_forces_containall_insertion() -> None:
    # Arrange
    cfg = AgentConfig(
        name="s",
        runtime="apptainer",
        workdir="/tmp/wd",
        labels={"groups": ["solver"]},
    )
    argv = ["apptainer", "exec", "--pwd", "/tmp/wd", "img.sif", "cmd"]
    # Act
    enforce_jail(cfg, argv, env={})
    # Assert
    assert argv[:3] == ["apptainer", "exec", "--containall"]
