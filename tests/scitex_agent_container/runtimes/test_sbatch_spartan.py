"""Regression tests for the hardened head-spartan sbatch wrapper generator.

Covers todo#425 — the 2026-04-14 silent short-exit chain where five
consecutive head-spartan jobs (23914805, 23916429, 23934176, 23936232,
23936277) completed with exit 0 in 15-36 seconds instead of holding
their multi-day SLURM allocation.

The root cause was a hand-edited sbatch wrapper whose fall-through
from the end of the script let ``set -e`` exit clean, SLURM reap the
cgroup, and the detached ``tmux new-session -d`` die with it. The
hardened generator in ``scitex_agent_container.runtimes.sbatch_spartan``
makes the persistent hold (``tail -f /dev/null``) unconditional and
gates the old capture-and-exit diagnostic branch behind an opt-in env
var. These tests guard every hardener against silent regression.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.runtimes.sbatch_spartan import (
    DIAGNOSTIC_ENV_VAR,
    REQUIRED_HOLD,
    REQUIRED_LOG_REDIRECT,
    REQUIRED_SHEBANG,
    REQUIRED_STRICT_MODE,
    REQUIRED_XTRACE,
    SpartanSbatchConfig,
    render_sbatch_script,
)


@pytest.fixture
def script() -> str:
    return render_sbatch_script()


def test_starts_with_bash_shebang(script: str) -> None:
    assert script.startswith(REQUIRED_SHEBANG + "\n"), (
        "sbatch wrapper must start with #!/bin/bash"
    )


def test_has_strict_mode(script: str) -> None:
    """todo#425 hardener #1 — set -euo pipefail must be present."""
    assert REQUIRED_STRICT_MODE in script, (
        "set -euo pipefail missing; silent failures will not crash loud"
    )


def test_has_log_redirect_before_main_body(script: str) -> None:
    """todo#425 hardener #2 — per-job log redirect for post-hoc diagnosis."""
    assert REQUIRED_LOG_REDIRECT in script, (
        "log redirect missing; next short-exit will be un-diagnosable"
    )
    # The redirect must happen before any real work (tmux, claude,
    # etc.) so a crash during setup is captured.
    redirect_pos = script.index(REQUIRED_LOG_REDIRECT)
    tmux_pos = script.index("tmux new-session")
    assert redirect_pos < tmux_pos, (
        "log redirect must occur before tmux spawn"
    )


def test_has_xtrace(script: str) -> None:
    """todo#425 hardener #2b — set -x follows the redirect."""
    assert REQUIRED_XTRACE in script, (
        "set -x missing; log will not show which line failed"
    )


def test_has_unconditional_tail_hold(script: str) -> None:
    """todo#425 hardener #3 — tail -f /dev/null must be the persistent hold.

    Specifically: the last non-trivial command in the wrapper must be
    ``tail -f /dev/null`` (or equivalent), NOT a conditional branch
    that can be skipped.
    """
    assert REQUIRED_HOLD in script, (
        "tail -f /dev/null missing; wrapper will fall through and short-exit"
    )
    # The hold must appear after any diagnostic-branch exit.
    hold_pos = script.rindex(REQUIRED_HOLD)
    diagnostic_exit_pos = script.rindex("exit 0")
    assert hold_pos > diagnostic_exit_pos, (
        "tail -f /dev/null must come AFTER the diagnostic branch exit, "
        "otherwise the diagnostic path can skip the hold and short-exit"
    )


def test_diagnostic_branch_is_opt_in(script: str) -> None:
    """todo#425 hardener #4 — diagnostic capture-and-exit is opt-in only.

    The old hand-edited wrapper dropped into a capture-and-exit branch
    by default. That branch MUST now be gated behind an explicit
    opt-in env var with default 0.
    """
    assert DIAGNOSTIC_ENV_VAR == "SCITEX_SPARTAN_DIAGNOSTIC"
    assert f'"${{{DIAGNOSTIC_ENV_VAR}:-0}}" = "1"' in script, (
        "diagnostic branch must be gated behind "
        f"{DIAGNOSTIC_ENV_VAR}=1 opt-in"
    )


def test_has_exit_trap_for_fall_through(script: str) -> None:
    """todo#425 hardener #5 — a trap on EXIT fails loud if the hold ever
    terminates, so a future regression can't silently re-introduce the
    short-exit pattern.
    """
    assert "trap" in script and "EXIT" in script, (
        "exit trap missing; wrapper regression would be silent again"
    )
    assert "todo#425" in script, (
        "trap/log message should reference todo#425 for forensics"
    )


def test_sbatch_directives_match_production(script: str) -> None:
    """Defaults match the 2026-04-14 production head-spartan sbatch."""
    assert "#SBATCH --partition=sapphire" in script
    assert "#SBATCH --time=7-00:00:00" in script
    assert "#SBATCH --cpus-per-task=2" in script
    assert "#SBATCH --mem=4G" in script
    assert "#SBATCH --job-name=head-spartan" in script
    assert "slurm_logs" in script


def test_cgroup_env_exports_present(script: str) -> None:
    """Agent identity env vars must survive into the tmux child."""
    assert 'CLAUDE_AGENT_ID="head-spartan"' in script
    assert 'SCITEX_OROCHI_AGENT="head-spartan"' in script
    assert "CLAUDE_DISABLE_AUTO_UPDATE=1" in script


def test_override_config_changes_only_explicit_fields() -> None:
    custom = SpartanSbatchConfig(
        job_name="mamba-healer-spartan",
        mem="8G",
        orochi_channels="#agent",
    )
    out = render_sbatch_script(custom)
    assert "#SBATCH --job-name=mamba-healer-spartan" in out
    assert "#SBATCH --mem=8G" in out
    assert 'SCITEX_OROCHI_CHANNELS="#agent"' in out
    # Hardeners still present under custom config
    assert REQUIRED_STRICT_MODE in out
    assert REQUIRED_HOLD in out
    assert REQUIRED_LOG_REDIRECT in out


def test_script_is_never_empty() -> None:
    out = render_sbatch_script()
    assert len(out) > 500, "rendered script is suspiciously small"
    assert out.count("\n") > 20, "rendered script has too few lines"


def test_no_naked_sleep_hold_pattern(script: str) -> None:
    """The old ``sleep $((31*24*3600-600))`` pattern is fragile (it can
    race-terminate at walltime boundary) and must not be reintroduced
    as the hold mechanism — ``tail -f /dev/null`` replaces it.
    """
    assert "sleep 604800" not in script, (
        "exec sleep 604800 pattern reintroduced; use tail -f /dev/null"
    )
    assert "31*24*3600" not in script, (
        "sleep $((31*24*3600-600)) pattern reintroduced; use tail -f /dev/null"
    )
