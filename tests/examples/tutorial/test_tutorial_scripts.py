"""Regression tests for ``examples/tutorial/*.sh``.

Three guarantees per script:

1. **Parseable** — ``bash -n <file>`` succeeds.
2. **Read-only safe** — without ``--apply`` every script exits 0 under a
   throwaway ``HOME`` (so failure of CLI lookups falls through ``|| true``
   guards rather than crashing).
3. **No stale CLI surface** — none of the scripts reference legacy
   command names, runtime flags, dockerfile paths, flat SIF layout, or
   "cross-runtime" claims. This is the regression guard for future
   CLI moves: rename a verb and at least one test here flips red.

``00_run_all.sh`` is parsed and lint-checked but not executed (it is
just an orchestrator that loops over its siblings).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

TUTORIAL_DIR = Path(__file__).resolve().parents[3] / "examples" / "tutorial"

# Numbered demo scripts (01..09) — safe to invoke without --apply.
NUMBERED_SCRIPTS = sorted(TUTORIAL_DIR.glob("0[1-9]_*.sh"))

# Every .sh in the tutorial directory, including the 00 orchestrator.
ALL_SCRIPTS = sorted(TUTORIAL_DIR.glob("*.sh"))

# Substrings that must never appear in any tutorial script: legacy CLI
# names, dropped runtime flags, dockerfile-era recipe filenames, and
# the old flat SIF naming.
STALE_PATTERNS = (
    "sac agent ",  # singular -> plural ('agents')
    "sac account ",  # account (singular) -> accounts (top-level plural)
    "runtime: docker",  # docker dropped 2026-05-13
    "runtime: podman",  # podman dropped 2026-05-13
    "--runtime docker",  # --runtime flag dropped
    "--runtime podman",
    "Dockerfile.base",  # only apptainer-{base,scitex}.def remain
    "Dockerfile.scitex",
    "scitex-agent-container-scitex.sif",  # flat layout -> sac-scitex/...
    "scitex-agent-container-base.sif",
    "scitex-agent-container-scitex.sandbox",
    "cross-runtime",  # apptainer-only since 2026-05-13
)


# ---------------------------------------------------------------------------
# 1. parse check
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script: Path) -> None:
    """``bash -n`` must accept every tutorial script."""
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"bash -n failed for {script.name}:\n{result.stderr}"


# ---------------------------------------------------------------------------
# 2. read-only execution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", NUMBERED_SCRIPTS, ids=lambda p: p.name)
def test_script_runs_readonly(script: Path, tmp_path: Path) -> None:
    """Running a script without --apply must exit 0 under a tmp HOME.

    Scripts perform only dry-run / status calls (``sac image list``,
    ``sac agents list``, echo statements). Where they invoke sac
    subcommands they tolerate failure via ``|| true``.
    """
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        # Preserve TERM so coloured-output libs don't crash on missing tty.
        "TERM": os.environ.get("TERM", "dumb"),
    }
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# 3. stale-CLI lint
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_script_has_no_stale_cli_strings(script: Path) -> None:
    """No tutorial script may reference legacy CLI surface."""
    text = script.read_text()
    offenders = [pat for pat in STALE_PATTERNS if pat in text]
    assert not offenders, f"{script.name} contains stale CLI strings: {offenders}"


def test_location_label_is_not_bare_LOCAL() -> None:
    """The old ``LOCAL`` location label has been replaced by
    ``host@host-workdir:container-workdir``. No tutorial script should
    advertise the legacy label as the expected output.

    We allow the substring inside larger words (`LOCALE`, `LOCAL_TZ`,
    etc.) — only the standalone token is forbidden.
    """
    import re

    pattern = re.compile(r"(?<![A-Z_])LOCAL(?![A-Z_])")
    for script in ALL_SCRIPTS:
        text = script.read_text()
        # Strip comment lines that explicitly say "old label was LOCAL" —
        # those are documentation of the change, not a stale claim.
        # The current scripts contain no such mention; this guard keeps
        # the test honest if someone re-introduces the discussion.
        matches = [
            line
            for line in text.splitlines()
            if pattern.search(line) and "old" not in line.lower()
        ]
        assert not matches, f"{script.name} mentions bare LOCAL label: {matches!r}"
