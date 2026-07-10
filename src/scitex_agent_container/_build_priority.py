"""Low-priority self-demotion for CPU/IO-heavy container image builds.

incident-local-heavy-build (2026-07-10): ``sac image build`` ran a full
SIF bake (apt + pip + mksquashfs, ~40 min of sustained CPU + IO) at
NORMAL priority on the operator's already-loaded interactive host — load
spiked 27 → 50+ and interactive latency tanked. Permanent fix #1: the
build path self-demotes BY DEFAULT so a bake only consumes CPU/IO the
host isn't using interactively. Dedicated build machines / CI opt out
explicitly (GitHub-hosted runners are single-purpose, so default-on is
harmless there — the opt-out just has to exist).

IO class choice — best-effort lowest, NOT idle (field-tested the same
night): a host SIF build run at ``ionice -c 3`` (idle class) died
silently at the "Creating SIF file..." (mksquashfs) stage on the loaded
host — process vanished, no error in the build log, no OOM trace, the
publish symlink never swapped. Idle-class IO is only serviced when the
disk is otherwise idle, so under sustained load it can starve
INDEFINITELY (and apparently got the squash stage killed or
wedged-then-reaped). Best-effort lowest (``ionice -c 2 -n 7``) still
yields to all higher-priority interactive IO but is guaranteed forward
progress; that is the setting this module bakes in.

Two demotion mechanisms, one per spawn shape:

* :func:`demote_current_process_to_low_priority` — for build work whose
  heavy subprocess is spawned BELOW an API boundary sac cannot reach.
  ``sac image build`` delegates to ``scitex_container.build()`` (a
  Python function of the separately-installed scitex-container package)
  whose internal ``subprocess.run(["apptainer", "build", ...])`` sac
  never sees, so an argv prefix is impossible there. Both the CPU nice
  value and the IO scheduling class/level are INHERITED across
  fork/exec, so demoting the calling process covers the entire
  descendant tree (apptainer build → apt/pip in %post → mksquashfs)
  plus the in-process build-context staging copy. Demotion is ONE-WAY
  for unprivileged processes (renicing back down needs CAP_SYS_NICE),
  so only call this from a short-lived CLI process that exits after the
  build — NEVER from a long-lived server (MCP server, listen daemon,
  agent runner).

* :func:`low_priority_build_prefix` — for ``apptainer build`` argvs sac
  composes itself (``runtimes/_apptainer_build.py``, the agent-start
  lazy SIF builds). Prefixing ``nice -n 19 ionice -c 2 -n 7`` demotes
  ONLY the spawned build; the calling process — and the agent container
  it goes on to launch — stays at normal priority.

CPU demotion is pure-stdlib (``os.setpriority``). IO demotion has no
stdlib binding, so both paths use util-linux ``ionice``; when ``ionice``
is not on PATH they degrade gracefully to nice-only with a warning
line, never a crash (macOS, minimal containers).

Opt-out surfaces: ``--no-nice`` on ``sac image build``, or
``SAC_BUILD_NO_NICE=1`` in the environment (covers the agent-start
build path too, and lets a dedicated build box disable self-demotion
fleet-wide without touching every invocation).
"""

from __future__ import annotations

import os
import shutil
import subprocess

BUILD_NICENESS = 19

# Best-effort class (2), lowest level (7) — deliberately NOT the idle
# class (3): idle-class IO can starve indefinitely on a loaded host and
# killed/wedged a real mksquashfs stage in the field (module docstring).
BUILD_IONICE_CLASS = "2"
BUILD_IONICE_LEVEL = "7"

# Environment opt-out — equivalent to ``--no-nice`` everywhere sac
# demotes a build. Any value other than empty/"0" disables demotion.
NO_NICE_ENV = "SAC_BUILD_NO_NICE"

# The loud one-line notices ``sac image build`` prints when self-demotion
# is active, so nobody is surprised by a slower build.
LOW_PRIORITY_NOTICE = (
    "building at low priority (nice 19 + ionice best-effort low); "
    "pass --no-nice for full speed"
)
LOW_PRIORITY_NOTICE_NICE_ONLY = (
    "building at low CPU priority (nice 19; ionice unavailable); "
    "pass --no-nice for full speed"
)


def _no_nice_env_set() -> bool:
    """``True`` when :data:`NO_NICE_ENV` requests normal-priority builds."""
    return os.environ.get(NO_NICE_ENV, "").strip() not in ("", "0")


def _set_io_low_on_self() -> str | None:
    """Move THIS process to best-effort-lowest IO priority.

    Shells out to ``ionice -c 2 -n 7 -p <own-pid>`` (there is no stdlib
    ``ioprio_set`` binding, and hand-rolling the syscall number is
    arch-fragile). Best-effort lowest, deliberately NOT the idle class —
    see the module docstring for the field incident behind that choice.
    Returns a one-line warning on graceful degrade (``ionice`` missing /
    failed — CPU nice still applies), ``None`` on success.
    """
    ionice = shutil.which("ionice")
    if ionice is None:
        return (
            "warning: ionice not found on PATH — IO priority stays at the "
            "default (CPU nice 19 still applies)"
        )
    result = subprocess.run(
        [
            ionice,
            "-c",
            BUILD_IONICE_CLASS,
            "-n",
            BUILD_IONICE_LEVEL,
            "-p",
            str(os.getpid()),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (
            f"warning: ionice -c {BUILD_IONICE_CLASS} -n {BUILD_IONICE_LEVEL} "
            f"failed (rc={result.returncode}: {result.stderr.strip()}) — IO "
            "priority stays at the default (CPU nice 19 still applies)"
        )
    return None


def demote_current_process_to_low_priority(*, skip: bool = False) -> list[str]:
    """Self-demote the CURRENT process to low CPU + IO priority.

    Sets CPU nice to :data:`BUILD_NICENESS` via ``os.setpriority`` and
    IO to best-effort lowest (``ionice -c 2 -n 7``) on our own PID —
    NOT the idle IO class, which starved/killed a real mksquashfs stage
    under load (module docstring). Every subprocess spawned afterwards
    inherits both, so the whole build tree (apptainer build → %post
    apt/pip → mksquashfs) runs at low priority without sac touching the
    spawn site.

    Returns the notice/warning lines the caller should print — the loud
    :data:`LOW_PRIORITY_NOTICE` one-liner on full success, a degrade
    warning plus :data:`LOW_PRIORITY_NOTICE_NICE_ONLY` when ``ionice``
    is unavailable, and an empty list when demotion was skipped
    (``skip=True`` from ``--no-nice``, or :data:`NO_NICE_ENV` set).

    One-way for unprivileged processes — call only from short-lived CLI
    processes that exit after the build (see module docstring).
    """
    if skip or _no_nice_env_set():
        return []
    try:
        os.setpriority(os.PRIO_PROCESS, 0, BUILD_NICENESS)
    except OSError as exc:
        return [
            f"warning: could not self-demote to nice {BUILD_NICENESS} "
            f"({exc}); building at normal priority"
        ]
    warning = _set_io_low_on_self()
    if warning is not None:
        return [warning, LOW_PRIORITY_NOTICE_NICE_ONLY]
    return [LOW_PRIORITY_NOTICE]


def low_priority_build_prefix() -> list[str]:
    """argv prefix that runs ONE spawned build at low priority.

    ``["nice", "-n", "19", "ionice", "-c", "2", "-n", "7"]`` when both
    tools are on PATH (best-effort lowest — never the starvation-prone
    idle class; see module docstring); degrades to nice-only when
    ``ionice`` is absent; empty (no demotion) when ``nice`` itself is
    absent or :data:`NO_NICE_ENV` opts out. Prepend to an ``apptainer
    build`` argv so only the build subprocess is demoted — the calling
    process keeps normal priority.
    """
    if _no_nice_env_set():
        return []
    if shutil.which("nice") is None:
        return []
    prefix = ["nice", "-n", str(BUILD_NICENESS)]
    if shutil.which("ionice") is not None:
        prefix += ["ionice", "-c", BUILD_IONICE_CLASS, "-n", BUILD_IONICE_LEVEL]
    return prefix


__all__ = [
    "BUILD_IONICE_CLASS",
    "BUILD_IONICE_LEVEL",
    "BUILD_NICENESS",
    "LOW_PRIORITY_NOTICE",
    "LOW_PRIORITY_NOTICE_NICE_ONLY",
    "NO_NICE_ENV",
    "demote_current_process_to_low_priority",
    "low_priority_build_prefix",
]
