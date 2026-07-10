"""Tests for ``_build_priority`` — low-priority build self-demotion.

incident-local-heavy-build: ``sac image build`` ran a full SIF bake at
normal priority on the loaded interactive host. The fix self-demotes the
build path (CPU nice 19 + IO best-effort lowest, ``ionice -c 2 -n 7``)
by default. NOT the idle IO class: a field build at ``-c 3`` starved and
died silently at the mksquashfs stage under sustained load.

No mocks (PA-306). Self-demotion is ONE-WAY for unprivileged processes,
so the REAL ``demote_current_process_to_low_priority`` behavior is
exercised in CHILD interpreters (``sys.executable -c``) with a curated
env — the pytest process itself is never demoted (tests/conftest.py
additionally sets ``SAC_BUILD_NO_NICE=1`` as suite-wide protection).
``low_priority_build_prefix`` only does PATH lookups, so it is tested
in-process against tmp bin dirs. AAA + ≥3-word names + one assert per
test (PA-307 / STX-TQ002).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scitex_agent_container import _build_priority as bp
from scitex_agent_container._build_priority import low_priority_build_prefix

_SRC_DIR = Path(bp.__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_child(code: str, *, env_overrides: dict[str, str | None]) -> str:
    """Run ``code`` in a fresh interpreter; return its stripped stdout.

    The child gets the current env with ``SAC_BUILD_NO_NICE`` REMOVED
    (tests/conftest.py sets it for suite hygiene; the child is where the
    real demotion is allowed to happen) and the package src dir on
    PYTHONPATH so it imports the same module under test. Entries in
    ``env_overrides`` with a ``None`` value are removed, others set.
    """
    env = os.environ.copy()
    env.pop(bp.NO_NICE_ENV, None)
    env["PYTHONPATH"] = str(_SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"child interpreter failed: {result.stderr}")
    return result.stdout.strip()


def _make_fake_exec(bin_dir: Path, name: str) -> None:
    """Drop an executable no-op shell script named ``name`` in ``bin_dir``."""
    script = bin_dir / name
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)


_IMPORT = (
    "from scitex_agent_container._build_priority "
    "import demote_current_process_to_low_priority"
)


# ---------------------------------------------------------------------------
# demote_current_process_to_low_priority — REAL demotion, seen in a child
# ---------------------------------------------------------------------------


def test_demote_sets_cpu_nice_19_on_calling_process() -> None:
    # Arrange
    code = (
        "import os\n"
        f"{_IMPORT}\n"
        "demote_current_process_to_low_priority()\n"
        "print(os.getpriority(os.PRIO_PROCESS, 0))\n"
    )
    # Act
    out = _run_child(code, env_overrides={})
    # Assert
    assert out == "19"


@pytest.mark.skipif(shutil.which("ionice") is None, reason="ionice not on PATH")
def test_demote_moves_calling_process_to_lowest_best_effort_io() -> None:
    # Arrange — after demotion, util-linux `ionice -p <pid>` reports the
    # process's IO scheduling class + level; best-effort lowest prints
    # "best-effort: prio 7". Deliberately NOT the idle class — idle IO
    # starved/killed a real mksquashfs stage under load.
    code = (
        "import os, subprocess\n"
        f"{_IMPORT}\n"
        "demote_current_process_to_low_priority()\n"
        "out = subprocess.run(['ionice', '-p', str(os.getpid())],\n"
        "                     capture_output=True, text=True)\n"
        "print(out.stdout.strip())\n"
    )
    # Act
    out = _run_child(code, env_overrides={})
    # Assert
    assert "best-effort" in out and "7" in out


def test_demote_returns_loud_notice_naming_no_nice_flag() -> None:
    # Arrange — the operator-facing one-liner must say the build is at
    # low priority AND name the opt-out flag, on both the full and the
    # ionice-degraded path.
    code = (
        f"{_IMPORT}\nprint('\\n'.join(demote_current_process_to_low_priority()))\n"
    )
    # Act
    out = _run_child(code, env_overrides={})
    # Assert
    assert "low" in out and "pass --no-nice for full speed" in out


def test_demote_env_opt_out_leaves_priority_untouched() -> None:
    # Arrange — SAC_BUILD_NO_NICE=1 must skip demotion entirely: no
    # notice lines AND an unchanged nice value.
    code = (
        "import os\n"
        f"{_IMPORT}\n"
        "before = os.getpriority(os.PRIO_PROCESS, 0)\n"
        "lines = demote_current_process_to_low_priority()\n"
        "after = os.getpriority(os.PRIO_PROCESS, 0)\n"
        "print(f'{lines}|{before == after}')\n"
    )
    # Act
    out = _run_child(code, env_overrides={bp.NO_NICE_ENV: "1"})
    # Assert
    assert out == "[]|True"


def test_demote_skip_kwarg_returns_no_lines_without_demoting() -> None:
    # Arrange — skip=True is the `--no-nice` CLI path; env cleared so
    # ONLY the kwarg drives the skip.
    code = (
        "import os\n"
        f"{_IMPORT}\n"
        "before = os.getpriority(os.PRIO_PROCESS, 0)\n"
        "lines = demote_current_process_to_low_priority(skip=True)\n"
        "after = os.getpriority(os.PRIO_PROCESS, 0)\n"
        "print(f'{lines}|{before == after}')\n"
    )
    # Act
    out = _run_child(code, env_overrides={})
    # Assert
    assert out == "[]|True"


# ---------------------------------------------------------------------------
# low_priority_build_prefix — argv prefix for builds sac spawns itself
# ---------------------------------------------------------------------------


def test_prefix_full_when_nice_and_ionice_on_path(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_exec(bin_dir, "nice")
    _make_fake_exec(bin_dir, "ionice")
    env_save_restore.delete(bp.NO_NICE_ENV)
    env_save_restore.set("PATH", str(bin_dir))
    # Act
    prefix = low_priority_build_prefix()
    # Assert
    assert prefix == ["nice", "-n", "19", "ionice", "-c", "2", "-n", "7"]


def test_prefix_degrades_to_nice_only_when_ionice_missing(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — graceful degrade, not a crash: minimal hosts without
    # util-linux still get CPU demotion.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_exec(bin_dir, "nice")
    env_save_restore.delete(bp.NO_NICE_ENV)
    env_save_restore.set("PATH", str(bin_dir))
    # Act
    prefix = low_priority_build_prefix()
    # Assert
    assert prefix == ["nice", "-n", "19"]


def test_prefix_empty_when_nice_itself_missing(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env_save_restore.delete(bp.NO_NICE_ENV)
    env_save_restore.set("PATH", str(bin_dir))
    # Act
    prefix = low_priority_build_prefix()
    # Assert
    assert prefix == []


def test_prefix_empty_under_env_opt_out(tmp_path: Path, env_save_restore) -> None:
    # Arrange — both tools present, but SAC_BUILD_NO_NICE=1 wins.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_exec(bin_dir, "nice")
    _make_fake_exec(bin_dir, "ionice")
    env_save_restore.set("PATH", str(bin_dir))
    env_save_restore.set(bp.NO_NICE_ENV, "1")
    # Act
    prefix = low_priority_build_prefix()
    # Assert
    assert prefix == []


def test_prefix_env_value_zero_means_no_opt_out(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — "0" is documented as NOT opting out (only empty/"0" keep
    # demotion on).
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_exec(bin_dir, "nice")
    _make_fake_exec(bin_dir, "ionice")
    env_save_restore.set("PATH", str(bin_dir))
    env_save_restore.set(bp.NO_NICE_ENV, "0")
    # Act
    prefix = low_priority_build_prefix()
    # Assert
    assert prefix == ["nice", "-n", "19", "ionice", "-c", "2", "-n", "7"]


# ---------------------------------------------------------------------------
# remote_build_advisory — remote-first warning on an already-loaded host
# ---------------------------------------------------------------------------


def test_advisory_silent_when_load_below_threshold() -> None:
    # Arrange — 8 cores, factor 1.5 -> threshold 12; load 10 is fine.
    load1, ncpu = 10.0, 8
    # Act
    advisory = bp.remote_build_advisory(load1=load1, ncpu=ncpu)
    # Assert
    assert advisory is None


def test_advisory_silent_at_exactly_the_threshold() -> None:
    # Arrange — boundary is inclusive-allow: load == factor x cores.
    load1, ncpu = 12.0, 8
    # Act
    advisory = bp.remote_build_advisory(load1=load1, ncpu=ncpu)
    # Assert
    assert advisory is None


def test_advisory_fires_in_the_incident_precondition_regime() -> None:
    # Arrange — the calibration point: the incident host sat at load
    # ~27 on 16 cores (~1.7x) BEFORE the bake started; the advisory
    # must already be loud there.
    load1, ncpu = 27.0, 16
    # Act
    advisory = bp.remote_build_advisory(load1=load1, ncpu=ncpu)
    # Assert
    assert advisory is not None


def test_advisory_names_remote_first_route_and_demoted_proceed() -> None:
    # Arrange — the warning must advise the remote/dedicated host
    # (Spartan) AND state the build proceeds demoted, so nobody reads
    # it as a refusal.
    load1, ncpu = 50.0, 12
    # Act
    advisory = bp.remote_build_advisory(load1=load1, ncpu=ncpu)
    # Assert
    assert "Spartan" in advisory and "DEMOTED" in advisory


def test_advisory_honours_custom_factor_parameter() -> None:
    # Arrange — factor 4.0 raises the bar: load 3x cores stays silent.
    load1, ncpu = 24.0, 8
    # Act
    advisory = bp.remote_build_advisory(load1=load1, ncpu=ncpu, factor=4.0)
    # Assert
    assert advisory is None


def test_advisory_guards_against_nonpositive_core_count() -> None:
    # Arrange — a bogus ncpu must clamp to 1, not turn the decision
    # into nonsense (threshold 2.0; load 5 fires).
    load1, ncpu = 5.0, 0
    # Act
    advisory = bp.remote_build_advisory(load1=load1, ncpu=ncpu)
    # Assert
    assert advisory is not None


def test_advisory_live_introspection_returns_str_or_none() -> None:
    # Arrange — no seams: exercise the real os.getloadavg/os.cpu_count
    # path; on any host the contract is "a string or None", never a
    # raise (advisory-only code must not crash a build).
    # Act
    advisory = bp.remote_build_advisory()
    # Assert
    assert advisory is None or isinstance(advisory, str)
