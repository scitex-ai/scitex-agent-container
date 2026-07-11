#!/usr/bin/env python3
"""POLICY for the heavy-job demotion guard hook (data, no logic).

The engine (``heavy_job_demotion_core.py``, driven by
``enforce_heavy_job_demotion.sh``) imports this module for: the heavy
command classes, the per-class EDUCATIONAL texts, the demotion prefix it
teaches, the env-knob names, and the block-message builder. Keeping the
policy separate means "add a command / reword an error" never touches
parsing logic (same split as the sibling ``hpc_login_hooks``).

WHY (P1 incident 2026-07-10, incident-local-heavy-build):
``sac image build`` ran a full SIF bake (apt + pip + mksquashfs) at
NORMAL priority on the operator's already-loaded shared interactive
host — load spiked 27 → 50+ and his session starved. The fleet lesson
is a PATTERN, not one command: ANY heavy CPU/IO job an agent launches
on a shared interactive host must yield to interactive work BY
DEFAULT. This hook is the guard for the pattern: a known-heavy command
without a ``nice``/``ionice`` prefix is blocked with the corrected
command, exactly like the env-dump / reload-watch guards — the guard,
not the memory.

Class rationale (what is denied, and why THESE):

* image builds (``apptainer|docker|podman|... build``) — the incident
  class itself when invoked directly (bypassing ``sac image build``'s
  built-in self-demotion, PR #605): ~15-40 min of sustained CPU + IO.
* ``mksquashfs`` — the exact stage that saturated the host, and the
  stage that DIED under idle-class IO (see ``DEMOTE_PREFIX`` below).
* mass compression (``xz``/``zstd``/``pigz``/…) and archive creation
  (``tar -c…``/``zip -r``/``7z a``) — sustained CPU + IO on whole
  trees. Plain single-threaded ``gzip``/``bzip2`` are deliberately NOT
  gated: one-file log compression is common, brief, and low-impact.
  Extraction (``tar x``) is likewise ungated — bursty and typically
  much shorter than creation.
* parallel builds (``make``/``ninja``/``cargo``/… with ``-j``/
  ``--jobs`` above ``SAC_HEAVY_JOB_JOBS_MAX``, bare ``-j``, or a
  dynamic ``-j$(nproc)``) — "cargo/gcc -j high": grabbing every core
  on the shared box. Serial / low-parallelism builds stay allowed.
* ``sac image build --no-nice`` — the explicit opt-out exists for
  DEDICATED build hosts (where this guard is disabled); using it on an
  interactive host re-creates the incident, so it is blocked here.
  Plain ``sac image build`` self-demotes and passes.
"""

from __future__ import annotations

import os

# The corrected prefix this hook teaches. IO class choice — best-effort
# lowest (``-c 2 -n 7``), deliberately NOT the idle class (``-c 3``):
# field-tested the night of the incident (2026-07-10), a host SIF build
# run at ``ionice -c 3`` starved and died silently at the mksquashfs
# stage under sustained load (idle-class IO is only serviced when the
# disk is otherwise idle, so it can starve INDEFINITELY); the retry at
# ``ionice -c 2 -n 7`` + ``nice 19`` completed fine. Best-effort lowest
# still yields to all interactive IO but is guaranteed forward progress.
# Keep in lockstep with scitex_agent_container._build_priority.
DEMOTE_PREFIX = "nice -n 19 ionice -c 2 -n 7"

# Env knobs (documented in README.md + the wrapper header).
ALLOW_ENV = "SAC_HEAVY_JOB_ALLOW"  # one-shot bypass (wrapper checks it)
DISABLE_ENV = "SAC_HEAVY_JOB_GUARD_DISABLE"  # standing dedicated-host opt-out
JOBS_MAX_ENV = "SAC_HEAVY_JOB_JOBS_MAX"  # -j threshold (default 4)
EXTRA_DENY_ENV = "SAC_HEAVY_JOB_EXTRA_DENY"  # extend the deny set per host
BYPASS_MARKER = "hook-bypass: heavy-job"  # inline per-command bypass

# Container-image builders gated on their SUBCOMMAND shape (first one or
# two non-flag args). Everything else these CLIs do (ps, exec, images,
# compose up, …) stays allowed.
IMAGE_BUILD_SUBCOMMANDS = {
    "apptainer": {("build",)},
    "singularity": {("build",)},
    "docker": {("build",), ("buildx", "build"), ("compose", "build")},
    "podman": {("build",), ("compose", "build")},
    "buildah": {("build",), ("bud",)},
    "nerdctl": {("build",)},
    "docker-compose": {("build",)},
}

# Unconditionally heavy binaries (any undemoted invocation blocks).
ALWAYS_HEAVY = {"mksquashfs": "squashfs", "unsquashfs": "squashfs"}

# Mass / parallel (de)compressors — any real invocation blocks
# (``--version`` / ``--help`` introspection passes). Plain gzip/bzip2
# deliberately excluded (module docstring).
COMPRESSORS = {
    "xz", "unxz", "pixz", "pigz", "unpigz", "pbzip2", "zstd", "unzstd",
    "pzstd", "lrzip", "lzma", "plzip", "lz4", "unlz4",
}

# Archive creators gated on their CREATE shape (extraction/list allowed).
SEVEN_ZIP = {"7z", "7za", "7zr"}

# Build orchestrators / compilers gated on high ``-j`` parallelism.
PARALLEL_BUILDERS = {
    "make", "gmake", "ninja", "cargo", "cmake", "ctest", "bazel",
    "mvn", "gradle", "gcc", "g++", "clang", "clang++", "rustc", "nvcc",
}

# Per-host deny extension, folded in by ``extend_deny_from_env()``.
EXTRA_DENY: set[str] = set()

EDU = {
    "image_build": (
        "  A container image build is a 15-40 min CPU+IO bake (deps, layers,\n"
        "  squashfs) -- the exact incident class.\n"
        "    - sac SIF layers: use `sac image build <layer>` (self-demotes by\n"
        "      default since PR #605)\n"
        "    - direct builder invocations must carry the demotion prefix\n"
    ),
    "squashfs": (
        "  mksquashfs is the stage that saturated the host in the incident --\n"
        "  sustained multi-core compression + heavy IO.\n"
    ),
    "compress": (
        "  Mass / parallel (de)compression saturates CPU and IO for the whole\n"
        "  host. (Plain single-file gzip/bzip2 stays ungated.)\n"
    ),
    "archive": (
        "  Creating an archive of a directory tree is sustained CPU+IO over\n"
        "  everything under it. (Extraction and listing stay ungated.)\n"
    ),
    "parallel_build": (
        "  High -j parallelism grabs every core on the shared box. Either\n"
        "  demote it, or cap the parallelism (-j<=$SAC_HEAVY_JOB_JOBS_MAX,\n"
        "  default 4) for an interactive-host-friendly build.\n"
    ),
    "sac_no_nice": (
        "  `--no-nice` / SAC_BUILD_NO_NICE exist for DEDICATED build hosts\n"
        "  (where this guard is disabled via SAC_HEAVY_JOB_GUARD_DISABLE=1).\n"
        "  On an interactive host, drop the flag -- `sac image build`\n"
        "  self-demotes by default, which is exactly what you want here.\n"
    ),
    "extra": (
        "  This command is on this host's extra deny list\n"
        "  ($SAC_HEAVY_JOB_EXTRA_DENY) -- the host owner marked it heavy.\n"
    ),
    "default": (
        "  This command class is known to saturate a shared host.\n"
    ),
}


def jobs_max() -> int:
    """The ``-j`` parallelism threshold (env-overridable, default 4)."""
    try:
        return int(os.environ.get(JOBS_MAX_ENV, "4") or "4")
    except ValueError:
        return 4


def guard_disabled() -> bool:
    """``True`` when :data:`DISABLE_ENV` opts this host out entirely.

    Any value other than empty/"0" disables — same semantics as
    ``SAC_BUILD_NO_NICE`` in ``_build_priority``.
    """
    return os.environ.get(DISABLE_ENV, "").strip() not in ("", "0")


def extend_deny_from_env() -> None:
    """Fold ``$SAC_HEAVY_JOB_EXTRA_DENY`` (comma/space list) into EXTRA_DENY."""
    import re

    for extra in re.split(r"[,\s]+", os.environ.get(EXTRA_DENY_ENV, "")):
        if extra.strip():
            EXTRA_DENY.add(extra.strip())


def block_message(bad_word: str, cls: str) -> str:
    """The full educational block message for one violation."""
    return (
        "BLOCKED by enforce_heavy_job_demotion.sh: '%s' is a known-HEAVY\n"
        "job launched WITHOUT nice/ionice on this shared interactive host.\n"
        "\n"
        "WHY THIS IS BLOCKED (P1 incident 2026-07-10):\n"
        "  A full SIF rebake ran at NORMAL priority on the operator's\n"
        "  already-loaded interactive host -- load spiked 27 -> 50+ and his\n"
        "  session starved. Heavy CPU/IO work on a shared interactive host\n"
        "  must yield to interactive work BY DEFAULT.\n"
        "\n"
        "%s"
        "\n"
        "DO ONE OF:\n"
        "  1. self-demote (usually all you need):\n"
        "       %s <your command>\n"
        "     NOTE: best-effort-lowest IO (-c 2 -n 7), NOT idle (-c 3) --\n"
        "     idle-class IO starved and killed a real mksquashfs stage under\n"
        "     load (field-tested 2026-07-10); best-effort-low still yields to\n"
        "     interactive IO but keeps forward progress.\n"
        "  2. prefer a REMOTE / dedicated build host (e.g. Spartan) for long\n"
        "     bakes -- heavy work does not belong on the shared box at all.\n"
        "  3. sac SIF builds: `sac image build <layer>` self-demotes by\n"
        "     default -- use it instead of raw apptainer.\n"
        "\n"
        "Host-level opt-out (dedicated build hosts): %s=1\n"
        "Bypass (rare -- operator-supervised):       %s=1\n"
        "  or append to the command:                 # %s\n"
        % (
            bad_word,
            EDU.get(cls, EDU["default"]),
            DEMOTE_PREFIX,
            DISABLE_ENV,
            ALLOW_ENV,
            BYPASS_MARKER,
        )
    )

# EOF
