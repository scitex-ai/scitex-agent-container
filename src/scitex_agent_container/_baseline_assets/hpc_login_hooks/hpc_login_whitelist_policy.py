#!/usr/bin/env python3
"""POLICY for the HPC login-node command whitelist hook (data, no logic).

The engine (``hpc_login_whitelist_core.py``, driven by
``enforce_hpc_login_node_whitelist.sh``) imports this module for: the
whitelist (``ALLOW``), the heavy-git gate (``GIT_HEAVY``), the blocked-
command classes (``CLASS_SETS``), the per-class EDUCATIONAL texts
(``EDU``) and the block-message builder. Keeping the policy separate
means "add a command / reword an error" never touches parsing logic.

Whitelist rationale (what a control-plane agent genuinely needs):

* SLURM verbs — the whole point of a login node. ``srun``/``sbatch``/
  ``salloc`` pass with ANY payload: the payload executes on a COMPUTE
  allocation, which is exactly the routing this hook teaches.
* ``module``/``ml`` — Lmod environment discovery+loading is control-plane.
* ``ssh``/``scp``/``rsync``/``sftp`` — launching AWAY from the node and
  small transfers (bulk data should still prefer a data-transfer node).
* ``git`` day-to-day — code sync is login work; object-store-heavy
  subcommands (``gc``/``fsck``/``repack``/…) are gated via ``GIT_HEAVY``.
* navigation/inspection + small-file text plumbing — an agent's bread
  and butter, O(small). ``du``/``find``/``ncdu`` are NOT whitelisted:
  recursive GPFS metadata scans are the 2026-06-09 incident class; ``fd``
  is the sanctioned fast lookup.
* ``tmux``/``screen`` — pane control, not compute.
* fleet CLIs (``sac``/``scitex-todo``/``scitex-hpc``/``gh``/…) — control
  plane by construction; ``scitex-hpc`` is the sanctioned job wrapper.
* ``curl``/``wget`` — API calls (big downloads belong in a job / DTN).
* ``python -c`` one-liners under ``$SAC_HPC_LOGIN_PYC_MAX`` (default 500
  chars) — tiny introspection, not compute (enforced by the engine).
"""

from __future__ import annotations

import os

PAT_ENV = "SAC_HPC_LOGIN_NODE_PATTERN"
DEFAULT_PATTERN = "spartan-login"

ALLOW = {
    # SLURM control plane; srun/sbatch/salloc payloads run on COMPUTE
    "squeue", "sbatch", "scancel", "sacct", "sinfo", "srun", "salloc",
    "scontrol", "sstat", "sprio", "sshare", "seff", "sattach", "sbcast",
    "sdiag", "sreport",
    # environment modules (Lmod)
    "module", "ml",
    # remote / small transfer
    "ssh", "scp", "rsync", "sftp",
    # git day-to-day (heavy subcommands gated via GIT_HEAVY)
    "git",
    # navigation / inspection / process control
    "ls", "pwd", "cd", "echo", "printf", "date", "hostname", "whoami",
    "id", "uname", "which", "type", "stat", "file", "readlink", "realpath",
    "dirname", "basename", "df", "free", "uptime", "ps", "kill", "pkill",
    "sleep", "true", "false", "test", "[",
    # small-file text plumbing (fd/rg sanctioned; du/find/ncdu are NOT)
    "cat", "head", "tail", "less", "more", "wc", "cut", "tr", "sort",
    "uniq", "diff", "grep", "egrep", "fgrep", "rg", "fd", "sed", "awk",
    "jq", "tee", "touch", "mkdir", "cp", "mv", "rm", "ln", "chmod",
    # multiplexers
    "tmux", "screen",
    # fleet control-plane CLIs
    "sac", "scitex-todo", "scitex-hpc", "scitex", "scitex-dev", "gh",
    "direnv",
    # light network/API
    "curl", "wget",
}

GIT_HEAVY = {
    "gc", "fsck", "repack", "prune", "filter-branch", "filter-repo",
    "fast-export", "fast-import", "pack-objects", "bundle",
}

CLASS_SETS = {
    "build_test": {
        "pytest", "tox", "make", "cmake", "ninja", "meson", "bazel", "gcc",
        "g++", "cc", "c++", "clang", "clang++", "ld", "nvcc", "mpicc",
        "mpicxx", "gfortran", "cargo", "rustc", "go", "javac", "mvn",
        "gradle",
    },
    "pkg_env": {
        "pip", "pip2", "pip3", "uv", "uvx", "pipx", "conda", "mamba",
        "micromamba", "poetry", "virtualenv", "npm", "npx", "pnpm", "yarn",
        "gem", "cpan", "apt", "apt-get", "dpkg", "yum", "dnf", "zypper",
        "brew", "spack",
    },
    "container": {
        "apptainer", "singularity", "docker", "podman", "docker-compose",
        "buildah",
    },
    "archive_io": {
        "tar", "gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zip",
        "unzip", "zstd", "unzstd", "7z", "7za", "pigz", "lz4", "split",
    },
    "fs_scan": {"du", "ncdu", "find", "tree", "locate", "updatedb"},
    "tex": {
        "pdflatex", "xelatex", "lualatex", "latexmk", "tectonic", "bibtex",
        "biber",
    },
    "interpreter": {
        "ipython", "jupyter", "jupyter-lab", "jupyter-notebook", "R",
        "Rscript", "matlab", "octave", "julia", "node", "perl", "ruby",
        "php",
    },
}

EDU = {
    "build_test": (
        "  Tests / builds / compilers are COMPUTE work, not login-node work.\n"
        "    - onto your existing allocation:  srun --overlap --jobid <JOBID> <cmd>\n"
        "    - as a batch job:                 sbatch --wrap '<cmd>'\n"
        "    - iterative dev loop:             scitex-hpc permanent session\n"
    ),
    "pkg_env": (
        "  Package/environment managers compile code and hammer the shared\n"
        "  filesystem -- the classic login-node offence.\n"
        "    - cluster software already exists: module load <name>\n"
        "      (discover versions: module spider <name>)\n"
        "    - env builds run INSIDE a job:  sbatch --wrap 'pip install ...'\n"
        "      or srun --overlap --jobid <JOBID> pip install ...\n"
        "    - persistent env work:          scitex-hpc permanent session\n"
    ),
    "container": (
        "  Container build/exec pulls gigabytes and burns CPU on the shared\n"
        "  node.\n"
        "    - sbatch --wrap 'apptainer build ...'                (batch)\n"
        "    - srun --overlap --jobid <JOBID> apptainer exec ...  (allocation)\n"
    ),
    "archive_io": (
        "  (De)compression / archiving is CPU+IO heavy on shared GPFS.\n"
        "    - run it inside a job:  sbatch --wrap 'tar czf ...'\n"
        "    - pure data movement:   use a data-transfer node, not the login node\n"
    ),
    "fs_scan": (
        "  Recursive filesystem scans hammer the shared GPFS metadata servers\n"
        "  for EVERY user (2026-06-09 incident: du/find on spartan-login).\n"
        "    - name lookups:   fd <pattern> <dir>   (whitelisted, fast)\n"
        "    - capacity:       df -h <mount>\n"
        "    - a real scan:    run it inside a job (sbatch --wrap 'du -sh ...')\n"
    ),
    "tex": (
        "  TeX compilation on the login node is the 2026-07-01 incident class.\n"
        "    - srun --overlap --jobid <JOBID> latexmk -pdf paper.tex\n"
        "    - or sbatch --wrap 'latexmk -pdf paper.tex'\n"
    ),
    "interpreter": (
        "  Long-running interpreters/scripts are exactly what admins kill on\n"
        "  login nodes.\n"
        "    - on your allocation:  srun --overlap --jobid <JOBID> python ...\n"
        "    - batch:               sbatch --wrap 'python train.py'\n"
        "    - interactive:         salloc / scitex-hpc permanent session\n"
        "    - tiny introspection stays allowed as python -c one-liners\n"
    ),
    "pyc_too_long": (
        "  This python -c payload exceeds the login-node one-liner size guard\n"
        "  ($SAC_HPC_LOGIN_PYC_MAX chars). A program that long is compute work:\n"
        "    - write it to a file and submit:  sbatch --wrap 'python prog.py'\n"
        "    - or run it on your allocation:   srun --overlap --jobid <JOBID> ...\n"
    ),
    "git_heavy": (
        "  This git subcommand rewrites/scans whole object stores -- heavy IO\n"
        "  on the shared filesystem. day-to-day git (status/log/pull/push/\n"
        "  commit/add/diff/...) stays whitelisted here.\n"
        "    - run it inside a job:  sbatch --wrap 'git -C <repo> gc'\n"
    ),
    "script": (
        "  A script is arbitrary work -- on a login node, SUBMIT it instead:\n"
        "    - sbatch <script>   (add #SBATCH headers)\n"
        "    - or run it on your allocation: srun --overlap --jobid <JOBID> bash <script>\n"
    ),
    "default": (
        "  If it computes, it belongs on a compute node.\n"
        "    - srun --overlap --jobid <JOBID> <cmd>   (existing allocation)\n"
        "    - sbatch --wrap '<cmd>'                  (batch)\n"
        "    - scitex-hpc permanent session           (persistent compute)\n"
    ),
}


def pyc_max() -> int:
    """The python ``-c`` one-liner size guard, chars (env-overridable)."""
    try:
        return int(os.environ.get("SAC_HPC_LOGIN_PYC_MAX", "500") or "500")
    except ValueError:
        return 500


def extend_allow_from_env() -> None:
    """Fold ``$SAC_HPC_LOGIN_EXTRA_ALLOW`` (comma/space list) into ALLOW."""
    import re

    for extra in re.split(
        r"[,\s]+", os.environ.get("SAC_HPC_LOGIN_EXTRA_ALLOW", "")
    ):
        if extra.strip():
            ALLOW.add(extra.strip())


def block_message(bad_word: str, cls: str, hostname: str, pattern: str) -> str:
    """The full educational block message for one violation."""
    return (
        "BLOCKED by enforce_hpc_login_node_whitelist.sh: '%s' is not on the\n"
        "HPC login-node whitelist (host '%s' matches $%s='%s').\n"
        "\n"
        "WHY THIS IS BLOCKED (operator directive 2026-07-10):\n"
        "  This is a SHARED HPC login node -- the cluster's front door for ALL\n"
        "  users. It is a CONTROL PLANE: submit jobs, watch queues, move small\n"
        "  files, edit code. Heavy CPU/RAM/IO here degrades everyone's session\n"
        "  and draws admin complaints (incidents: 2026-06-09 du/find scan,\n"
        "  2026-07-01 TeX compile). Run the WORK on compute nodes.\n"
        "\n"
        "%s"
        "\n"
        "THE ROUTES OFF THE LOGIN NODE:\n"
        "  1. existing allocation:  srun --overlap --jobid <JOBID> <cmd>\n"
        "  2. batch job:            sbatch --wrap '<cmd>'  (or an #SBATCH script)\n"
        "  3. interactive session:  salloc / scitex-hpc permanent session\n"
        "  4. cluster software:     module load <name>  (module spider <name>)\n"
        "\n"
        "Login-node whitelist (control-plane): SLURM verbs (squeue/sbatch/\n"
        "scancel/sacct/sinfo/srun/salloc/scontrol/...), module/ml, ssh/scp/\n"
        "rsync/sftp, git (day-to-day), tmux/screen, sac/scitex-todo/scitex-hpc/\n"
        "gh, curl/wget (API), ls/cat/head/tail/grep/rg/fd/sed/awk/jq/..., and\n"
        "python -c one-liners up to %d chars.\n"
        "\n"
        "Extend for this host (login-safe cmds): SAC_HPC_LOGIN_EXTRA_ALLOW='a,b'\n"
        "Bypass (rare -- operator-supervised):   SAC_HPC_LOGIN_ALLOW=1\n"
        "  or append to the command:             # hook-bypass: hpc-login\n"
        % (bad_word, hostname, PAT_ENV, pattern, EDU.get(cls, EDU["default"]),
           pyc_max())
    )

# EOF
