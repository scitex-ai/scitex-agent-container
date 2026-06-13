"""``sac pytest spartan run`` — submit + collect a Spartan SLURM pytest job.

Phase 1 of the "Spartan pytest runner" operator directive (2026-06-13):
operator wants to STOP running the full pytest suite on their laptop.
Instead, this command submits ``<repo>@<branch>`` to Spartan's CPU
reservation, waits for the SLURM job, and returns pass/fail + a failure
summary. Used as a pre-push fast-verification surface so the broad
suite runs on Spartan, not on the laptop's CPU.

Architecture::

  laptop                                       Spartan
  ┌──────────────────────┐                    ┌─────────────────────────┐
  │ sac pytest spartan   │  ssh + sbatch      │  scratch clone          │
  │ run REPO@BRANCH      │ ─────────────────► │  pip install -e .[dev]  │
  │ (this module)        │                    │  pytest → summary.json  │
  │   poll squeue        │ ◄──────────────────│  squeue COMPLETED       │
  │   cat summary.json   │ ◄──────────────────│  summary.json           │
  │   parse + print      │                    │                         │
  └──────────────────────┘                    └─────────────────────────┘

Phase 1 simplifications (each is a Phase 2 follow-up):

* sbatch script generated inline as a heredoc; no template file.
  **Phase 2**: factor into ``sbatch_templates/spartan_pytest.sh`` so
  operators can override the install / pytest flags without code edits.
* SLURM reservation name hardcoded to ``sapphire`` (operator's CPU pool)
  but exposed via ``--reservation``. **Phase 2**: read default from
  ``~/.scitex/agent-container/config.yaml`` (``spartan_pytest.reservation``)
  so the operator's default never has to live on the command line.
* No retry on transient SSH errors; first failure surfaces.
  **Phase 2**: wrap the ssh+scp legs in a small exponential-backoff
  retry (3 attempts) for HPC login-node hiccups.
* No incremental log streaming; only the final summary.
  **Phase 2**: ``--follow`` to ``tail -F`` the SLURM out file via ssh
  while polling.
* No auto pre-push hook installer.
  **Phase 2**: ``sac pytest spartan install-pre-push`` writes
  ``.git/hooks/pre-push`` that runs this command against the
  about-to-be-pushed branch and blocks the push on failure.

Public API is re-exported here so callers can keep a single import
path; the implementation is split across :mod:`_summary`, :mod:`_render`,
:mod:`_ssh`, and :mod:`_run_cmd` to stay under the per-file line cap.
"""

from __future__ import annotations

from ._render import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_RESERVATION,
    DEFAULT_SSH_HOST,
    DEFAULT_TIMEOUT_S,
    _render_sbatch_script,
)
from ._run_cmd import pytest_group, run_cmd, spartan_group
from ._ssh import (
    _extract_job_id,
    _fetch_summary,
    _poll_job,
    _run_ssh,
    _submit_sbatch,
)
from ._summary import (
    PytestSummary,
    _format_summary,
    _parse_summary,
    _resolve_exit_code,
    _split_repo_at_branch,
)

__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_RESERVATION",
    "DEFAULT_SSH_HOST",
    "DEFAULT_TIMEOUT_S",
    "PytestSummary",
    "_extract_job_id",
    "_fetch_summary",
    "_format_summary",
    "_parse_summary",
    "_poll_job",
    "_render_sbatch_script",
    "_resolve_exit_code",
    "_run_ssh",
    "_split_repo_at_branch",
    "_submit_sbatch",
    "pytest_group",
    "run_cmd",
    "spartan_group",
]
