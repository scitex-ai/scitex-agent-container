"""D2 — static $HOME-visibility preflight tests.

See docs/adr/0001-isolation-hardening.md (D2 + D4).
"""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ApptainerSpec
from scitex_agent_container.runtimes._apptainer_preflight import PREFLIGHT_SCRIPT
from scitex_agent_container.runtimes._apptainer_runtime import (
    ApptainerContainerRuntime,
)


def _cfg(workdir: Path, **kw) -> AgentConfig:
    # tmpfs_size="" is DECLARED here, never inherited.
    #
    # Every test in this file asserts on the INNER argv (everything
    # after the SIF path). ``--workdir`` is an OUTER flag and no
    # assertion here reads it. But building the argv calls
    # ``tmpfs_workdir_flags``, which runs ``shutil.disk_usage`` against
    # the real filesystem and raises ``TmpfsSpaceError`` when free space
    # is below ``tmpfs_size``. Under the 2G default that makes these
    # verdicts depend on ambient free disk the tests neither control nor
    # are about.
    #
    # 2026-08-19: a shared CI runner at 91% full turned that dependency
    # into named, code-shaped failures here — test_preflight_wrapper_*
    # and test_preflight_script_* all went red on a full disk, which
    # sends readers to their own diff for a condition no diff caused.
    #
    # The guard itself is correct and stays. It is covered
    # deterministically in ``test__apptainer_tmpfs.py``, which requests
    # 10 EiB so it fires on any host rather than only on a full one.
    #
    # Normalise on EVERY path, including callers that pass their own
    # spec — a setdefault would leave those reinheriting the 2G default.
    ap = kw.pop("apptainer", None) or ApptainerSpec()
    kw["apptainer"] = replace(ap, tmpfs_size="")
    return AgentConfig(
        name=kw.pop("name", "iso"),
        runtime="apptainer",
        workdir=str(workdir),
        **kw,
    )


def _inner_after_sif(argv: list[str], sif: Path) -> list[str]:
    return argv[argv.index(str(sif)) + 1 :]


def _unwrap_git_alias_shell(tokens: list[str]) -> list[str]:
    """Peel the unconditional ``/bin/bash -lc "<git-alias>; exec <inner>"``
    wrapper (see ``_apptainer_inner_argv._GIT_ENV_ALIAS_STEPS``) if present.

    Every agent's inner argv is now wrapped in this alias shell regardless
    of ``startup_commands``/``relaxed``, so both the "relaxed" (no D2
    preflight) and the D2-wrapped tricky-quoting fixtures need one more
    unwrap step than before that step existed.
    """
    if len(tokens) >= 3 and tokens[0] == "/bin/bash" and tokens[1] == "-lc":
        _, _, exec_line = tokens[2].rpartition("; exec ")
        return shlex.split(exec_line)
    return tokens


# ---------------------------------------------------------------------------
# Fixtures: default-wrapped invocation (preflight active)
# ---------------------------------------------------------------------------


@pytest.fixture
def default_inner(tmp_path: Path) -> list[str]:
    # Arrange
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    cfg = _cfg(tmp_path, startup_prompts=["go"])
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    # Assert (handed to caller test)
    return _inner_after_sif(argv, sif)


@pytest.fixture
def default_script(default_inner: list[str]) -> str:
    return default_inner[2]


# ---------------------------------------------------------------------------
# Default invocation is wrapped: ["bash", "-c", "<preflight>\nexec <inner>"]
# ---------------------------------------------------------------------------


def test_preflight_wrapper_invokes_bash(default_inner: list[str]) -> None:
    # Arrange
    inner = default_inner
    # Act
    program = inner[0]
    # Assert
    assert program == "bash"


def test_preflight_wrapper_uses_dash_c_flag(default_inner: list[str]) -> None:
    # Arrange
    inner = default_inner
    # Act
    flag = inner[1]
    # Assert
    assert flag == "-c"


def test_preflight_script_checks_root_uid(default_script: str) -> None:
    # Arrange
    script = default_script
    # Act
    has_uid_check = '[ "$(id -u)" = "0" ]' in script
    # Assert
    assert has_uid_check


def test_preflight_script_reads_uid_map(default_script: str) -> None:
    # Arrange
    script = default_script
    # Act
    has_uid_map = "/proc/self/uid_map" in script
    # Assert
    assert has_uid_map


def test_preflight_script_asserts_home_path(default_script: str) -> None:
    # Arrange
    script = default_script
    # Act
    has_home_assert = 'test "$HOME" = "/home/agent"' in script
    # Assert
    assert has_home_assert


def test_preflight_script_execs_inner_so_pid1_is_tini(default_script: str) -> None:
    # Arrange
    script = default_script
    # Act
    exec_line_present = "\nexec " in script and "/usr/bin/tini" in script
    # Assert
    assert exec_line_present


# ---------------------------------------------------------------------------
# Relaxed mode: no preflight wrapper at all
# ---------------------------------------------------------------------------


@pytest.fixture
def relaxed_argv(tmp_path: Path) -> tuple[list[str], list[str]]:
    # Arrange
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    cfg = _cfg(
        tmp_path,
        apptainer=ApptainerSpec(relaxed=True),
        startup_prompts=["go"],
    )
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    inner = _inner_after_sif(argv, sif)
    # Assert (handed to caller test)
    return argv, inner


def test_preflight_relaxed_inner_starts_with_tini(
    relaxed_argv: tuple[list[str], list[str]],
) -> None:
    # Arrange
    _argv, inner = relaxed_argv
    # Act
    first = _unwrap_git_alias_shell(inner)[0]
    # Assert
    assert first == "/usr/bin/tini"


def test_preflight_relaxed_omits_uid_map_check(
    relaxed_argv: tuple[list[str], list[str]],
) -> None:
    # Arrange
    argv, _inner = relaxed_argv
    # Act
    joined = "\n".join(argv)
    # Assert
    assert "/proc/self/uid_map" not in joined


def test_preflight_relaxed_omits_home_check(
    relaxed_argv: tuple[list[str], list[str]],
) -> None:
    # Arrange
    argv, _inner = relaxed_argv
    # Act
    joined = "\n".join(argv)
    # Assert
    assert 'test "$HOME" = "/home/agent"' not in joined


# ---------------------------------------------------------------------------
# Quoting: tricky mission strings must round-trip through bash -c
# ---------------------------------------------------------------------------


TRICKY_MISSION = "hello 'world'; echo $PATH \"oops\""


@pytest.fixture
def tricky_inner(tmp_path: Path) -> list[str]:
    # Arrange
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    cfg = _cfg(tmp_path, startup_prompts=[TRICKY_MISSION])
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    # Assert (handed to caller test)
    return _inner_after_sif(argv, sif)


@pytest.fixture
def tricky_exec_argv(tricky_inner: list[str]) -> list[str]:
    script = tricky_inner[2]
    _, _, exec_line = script.rpartition("\nexec ")
    # The D2 preflight's own exec line is itself the unconditional
    # ``/bin/bash -lc "<git-alias>; exec <inner>"`` wrapper — one more
    # unwrap is needed to reach the actual tini/mission invocation.
    return _unwrap_git_alias_shell(shlex.split(exec_line))


def test_preflight_quoting_wraps_with_bash_dash_c(tricky_inner: list[str]) -> None:
    # Arrange
    inner = tricky_inner
    # Act
    prefix = inner[0:2]
    # Assert
    assert prefix == ["bash", "-c"]


def test_preflight_quoting_mission_round_trips_intact(
    tricky_exec_argv: list[str],
) -> None:
    # Arrange
    parsed = tricky_exec_argv
    # Act
    survived = TRICKY_MISSION in parsed
    # Assert
    assert survived


def test_preflight_quoting_preserves_tini_in_exec_line(
    tricky_exec_argv: list[str],
) -> None:
    # Arrange
    parsed = tricky_exec_argv
    # Act
    has_tini = "/usr/bin/tini" in parsed
    # Assert
    assert has_tini


# ---------------------------------------------------------------------------
# Static module constant (ADR §D4 — single sha256, verifiable by Clew)
# ---------------------------------------------------------------------------


def test_preflight_constant_is_a_string() -> None:
    # Arrange
    constant = PREFLIGHT_SCRIPT
    # Act
    kind = type(constant)
    # Assert
    assert kind is str


def test_preflight_constant_contains_home_assert() -> None:
    # Arrange
    constant = PREFLIGHT_SCRIPT
    # Act
    has_home_assert = 'test "$HOME" = "/home/agent"' in constant
    # Assert
    assert has_home_assert


def test_preflight_constant_contains_uid_map_check() -> None:
    # Arrange
    constant = PREFLIGHT_SCRIPT
    # Act
    has_uid_map = "/proc/self/uid_map" in constant
    # Assert
    assert has_uid_map


def test_preflight_constant_contains_root_uid_check() -> None:
    # Arrange
    constant = PREFLIGHT_SCRIPT
    # Act
    has_id_u = "id -u" in constant
    # Assert
    assert has_id_u


# ---------------------------------------------------------------------------
# Overlay does NOT disable preflight (only --writable-tmpfs is skipped)
# ---------------------------------------------------------------------------


@pytest.fixture
def overlay_inner(tmp_path: Path) -> list[str]:
    # Arrange — pre-create the overlay file so the existence check in
    # build_run_argv passes without exercising auto-create.
    rt = ApptainerContainerRuntime()
    sif = tmp_path / "x.sif"
    overlay = tmp_path / "ov.img"
    overlay.write_bytes(b"")
    cfg = _cfg(tmp_path, apptainer=ApptainerSpec(overlay=str(overlay)))
    # Act
    argv = rt.build_run_argv(cfg, state_dir=tmp_path, sif_path=sif)
    # Assert (handed to caller test)
    return _inner_after_sif(argv, sif)


def test_preflight_with_overlay_still_wraps_with_bash_dash_c(
    overlay_inner: list[str],
) -> None:
    # Arrange
    inner = overlay_inner
    # Act
    prefix = inner[0:2]
    # Assert
    assert prefix == ["bash", "-c"]


def test_preflight_with_overlay_still_asserts_home_path(
    overlay_inner: list[str],
) -> None:
    # Arrange
    script = overlay_inner[2]
    # Act
    has_home_assert = 'test "$HOME" = "/home/agent"' in script
    # Assert
    assert has_home_assert
