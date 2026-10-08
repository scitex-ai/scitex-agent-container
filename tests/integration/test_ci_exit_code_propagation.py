"""Inner-script exit codes must propagate to the GitHub step verdict.

``exec-in-sif.sh`` ends in ``exec apptainer ... bash .github/ci/<inner>`` and
``run-in-sif.sh`` ends in ``exec ... pytest``. That ``exec`` chain is what
turns a red suite into a red step; if any layer swallowed the code, CI
would report green on failure.

These tests drive the REAL ``exec-in-sif.sh`` with a fake ``apptainer``
on PATH (same argv contract: ``exec ... <sif> bash .github/ci/<inner>
<args>``) plus a committed probe inner script, and assert the outer
exit code equals the inner one. No mocks / monkeypatch (STX-NM):
real script, real subprocess, real exit codes.

Conventions: one assertion per test (STX-TQ007); AAA markers.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_DIR = REPO_ROOT / ".github" / "ci"

_FAKE_APPTAINER = """#!/bin/sh
# Test double for `apptainer`: consume argv up to the *.sif image, then
# exec the remainder (`bash .github/ci/<inner> <args>`). Mirrors the real
# binary's contract closely enough to prove exit-code propagation through
# exec-in-sif.sh's final `exec` handoff.
while [ $# -gt 0 ]; do
    case "$1" in
        *.sif) shift; break ;;
        *) shift ;;
    esac
done
exec "$@"
"""


def _run_outer(tmp_path, *inner_argv):
    """Run the real exec-in-sif.sh against the fake apptainer; return CompletedProcess."""
    # Arrange is done by the caller; this helper only owns the shared rig.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    apptainer = fake_bin / "apptainer"
    apptainer.write_text(_FAKE_APPTAINER)
    apptainer.chmod(0o755)
    sif = tmp_path / "ci-cpu.sif"
    sif.write_bytes(b"fake-sif-stand-in")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)  # never reap outside CI, per the script
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
    env["SCITEX_CI_SIF"] = str(sif)
    env["SAC_CI_TMPDIR_ROOT"] = str(scratch)
    env["HOME"] = str(tmp_path)  # isolate ~/.scitex/pg + ~/.env-3.11 lookups
    return subprocess.run(
        ["bash", str(CI_DIR / "exec-in-sif.sh"), *inner_argv],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_inner_exit_42_propagates_to_outer_step(tmp_path):
    # Arrange — the probe inner script exits 42; GITHUB_ACTIONS is unset
    # inside the helper so the process-reap block cannot fire.
    # Act
    proc = _run_outer(tmp_path, "exit-code-probe.sh", "42")
    # Assert
    assert proc.returncode == 42


def test_inner_exit_zero_stays_zero(tmp_path):
    # Arrange — a green inner script must stay green through the handoff.
    # Act
    proc = _run_outer(tmp_path, "exit-code-probe.sh", "0")
    # Assert
    assert proc.returncode == 0
