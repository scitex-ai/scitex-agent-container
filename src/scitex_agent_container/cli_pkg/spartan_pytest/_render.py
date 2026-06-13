"""Sbatch-script renderer + operator-tunable defaults.

Pure string templating: no IO, no subprocess.  See the package
docstring in ``__init__.py`` for the full architecture and the Phase-2
follow-ups this module deliberately defers.
"""

from __future__ import annotations

import shlex

# ---------------------------------------------------------------------------
# Constants — operator-tunable defaults.
# ---------------------------------------------------------------------------

#: SSH alias the operator's ``~/.ssh/config`` resolves to Spartan's login
#: node.  Operator override via ``--ssh-host`` keeps a no-config flow
#: working out of the box.
DEFAULT_SSH_HOST = "spartan"

#: SLURM reservation hardcoded for Phase 1 — operator's sapphire CPU pool.
#: ``--reservation`` lets ops point at any reservation without code edits.
DEFAULT_RESERVATION = "sapphire"

#: Default poll-loop timeout.  Spartan's sapphire reservation tends to
#: schedule within a couple minutes; 30 min is generous headroom.
DEFAULT_TIMEOUT_S = 30 * 60

#: Seconds between successive ``squeue`` polls.  Tight enough to feel
#: responsive on the operator's terminal; loose enough not to hammer
#: Spartan's login node.
DEFAULT_POLL_INTERVAL_S = 15


def _render_sbatch_script(
    *,
    repo: str,
    branch: str,
    reservation: str,
    scratch_dir: str,
    job_tag: str,
) -> str:
    """Return the sbatch shell script body for one Spartan pytest job.

    Pure string template — no IO.  Caller is responsible for writing
    this to a file (or piping into ``sbatch`` over ssh) and reading
    back ``<scratch_dir>/summary.json`` once the job completes.

    The script:

    1. Clones ``repo`` (any git-reachable URL or ``owner/name``
       shorthand resolved via the operator's gh credentials on Spartan).
    2. Checks out ``branch``.
    3. ``pip install -e .[dev]``.
    4. Runs ``pytest -q --no-cov --maxfail=20``, capturing pass/fail
       counts to ``summary.json``.
    """
    scratch_repr = repr(scratch_dir)
    quoted_scratch = shlex.quote(scratch_dir)
    quoted_repo = shlex.quote(repo)
    quoted_branch = shlex.quote(branch)
    return (
        "#!/bin/bash\n"
        f"#SBATCH --job-name=sac-pytest-{job_tag}\n"
        f"#SBATCH --reservation={reservation}\n"
        f"#SBATCH --output={scratch_dir}/slurm-%j.out\n"
        f"#SBATCH --error={scratch_dir}/slurm-%j.err\n"
        "#SBATCH --time=00:30:00\n"
        "#SBATCH --cpus-per-task=4\n"
        "#SBATCH --mem=8G\n"
        "\n"
        "set -eo pipefail\n"
        f"mkdir -p {quoted_scratch}\n"
        f"cd {quoted_scratch}\n"
        "\n"
        "# Clone + checkout — accept full URL OR owner/name shorthand.\n"
        f"REPO={quoted_repo}\n"
        'if [[ "$REPO" == */* && "$REPO" != *://* && "$REPO" != git@* ]]; then\n'
        '    REPO_URL="https://github.com/$REPO.git"\n'
        "else\n"
        '    REPO_URL="$REPO"\n'
        "fi\n"
        'git clone --depth 50 "$REPO_URL" checkout\n'
        "cd checkout\n"
        f"git checkout {quoted_branch}\n"
        "\n"
        "# Install + test.  dev extra is the standard SciTeX convention;\n"
        "# falling back to plain ``-e .`` lets repos without a dev extra\n"
        "# still run.\n"
        "python -m pip install --quiet -e '.[dev]' "
        "|| python -m pip install --quiet -e .\n"
        "\n"
        "START_TS=$(date +%s)\n"
        "set +e\n"
        "python -m pytest -q --no-cov --maxfail=20 2>&1 "
        f"| tee {quoted_scratch}/pytest.log\n"
        "PYTEST_EXIT=$?\n"
        "set -e\n"
        "END_TS=$(date +%s)\n"
        "DURATION=$((END_TS - START_TS))\n"
        "\n"
        "# Extract pass/fail counts from the captured log.  Pytest's\n"
        "# terminal summary line is the canonical source; a small\n"
        "# Python helper keeps the sbatch script portable across\n"
        "# pytest versions (no pytest-json-report dep required).\n"
        "python - <<PYEOF\n"
        "import json, re, pathlib\n"
        f"log_path = pathlib.Path({scratch_repr}) / 'pytest.log'\n"
        "log = log_path.read_text(errors='replace') if log_path.exists() else ''\n"
        "passed = failed = errors = 0\n"
        "for word, label in (('passed', 'passed'), ('failed', 'failed'), "
        "('error', 'errors')):\n"
        "    m = re.search(rf'(\\d+) {word}', log)\n"
        "    if m:\n"
        "        if label == 'passed':\n"
        "            passed = int(m.group(1))\n"
        "        elif label == 'failed':\n"
        "            failed = int(m.group(1))\n"
        "        else:\n"
        "            errors = int(m.group(1))\n"
        "failed_tests = re.findall(r'^FAILED (\\S+)', log, re.MULTILINE)\n"
        "summary = {\n"
        "    'passed': passed,\n"
        "    'failed': failed,\n"
        "    'errors': errors,\n"
        "    'duration_s': $DURATION,\n"
        "    'failed_tests': failed_tests,\n"
        "}\n"
        f"out = pathlib.Path({scratch_repr}) / 'summary.json'\n"
        "out.write_text(json.dumps(summary, indent=2))\n"
        "PYEOF\n"
        "\n"
        "exit $PYTEST_EXIT\n"
    )


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_RESERVATION",
    "DEFAULT_SSH_HOST",
    "DEFAULT_TIMEOUT_S",
    "_render_sbatch_script",
]
