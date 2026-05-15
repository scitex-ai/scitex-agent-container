"""E2E: bulk-start multiple agents from a fleet-style directory.

What this test covers
=====================
``sac agents start`` accepts either a single agent name / YAML path or
an entire directory (``~/.scitex/agent-container/agents/`` style) in
which every ``<name>/<name>.yaml`` (or ``<name>/spec.yaml``) becomes
one start invocation. This is the local-host equivalent of
``sac fleet launch`` and is the path real operators take when standing
up a fleet on one box.

Workflow
--------
1. Synthesize a fleet-template directory with two agent spec dirs
   pointing at the real apptainer base image.
2. ``sac agents start <fleet_dir>`` — one bulk invocation.
3. Verify both agent names appear in ``sac agents list --json`` and
   reach a non-error state.

Skip strategy
-------------
* Module-level ``pytest.mark.e2e``.
* ``RUN_E2E`` env gate from conftest.
* Skipif when ``apptainer`` binary or base image is unavailable.

Cleanup is unconditional in the fixture teardown so a failing assert
does not leak two long-lived agents into the operator's environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("RUN_E2E"), reason="E2E disabled by default"),
    pytest.mark.skipif(
        shutil.which("apptainer") is None,
        reason="apptainer binary not on PATH",
    ),
]


# ---------------------------------------------------------------------------
# Helpers — materialise N agent specs under a shared fleet directory.
# ---------------------------------------------------------------------------


def _write_fleet(home: Path, names: list[str]) -> Path:
    """Build a fleet directory with one spec.yaml per ``name``."""
    fleet_root = home / ".scitex/agent-container/agents"
    fleet_root.mkdir(parents=True, exist_ok=True)
    image = home / ".scitex/agent-container/containers/sac-base.sif"
    for name in names:
        agent_dir = fleet_root / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "spec.yaml").write_text(
            textwrap.dedent(
                f"""\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    role: e2e-fleet-bulk
                spec:
                  runtime: apptainer
                  apptainer:
                    image: {image}
                  claude:
                    model: haiku
                    flags:
                      - --dangerously-skip-permissions
                    session: new-session
                """
            )
        )
    return fleet_root


# ---------------------------------------------------------------------------
# Fixture — build a 2-agent fleet, bulk-start, yield names; stop on teardown.
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_fleet_scenario(
    sac_bin: str,
    apptainer_available: bool,
    tmp_home_with_image: Path,
):
    if not apptainer_available:
        pytest.skip("apptainer + sac-base.sif unavailable on this host")

    suffix = uuid.uuid4().hex[:6]
    names = [f"sac-e2e-fleet-{suffix}-a", f"sac-e2e-fleet-{suffix}-b"]
    fleet_root = _write_fleet(tmp_home_with_image, names)
    env = {**os.environ, "HOME": str(tmp_home_with_image)}

    bulk_proc = subprocess.run(
        [sac_bin, "agents", "start", str(fleet_root), "--no-preflight", "-y"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    try:
        yield {
            "names": names,
            "fleet_root": fleet_root,
            "start": bulk_proc,
            "env": env,
        }
    finally:
        for name in names:
            subprocess.run(
                [sac_bin, "agents", "stop", name, "--force", "-y"],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )


def _registry_snapshot(sac: str, env: dict) -> dict:
    proc = subprocess.run(
        [sac, "agents", "list", "--json"],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"agents": []}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"agents": []}


# ---------------------------------------------------------------------------
# Assertions — one observable behaviour per test.
# ---------------------------------------------------------------------------


def test_bulk_start_command_exits_successfully(
    bulk_fleet_scenario: dict,
) -> None:
    # Arrange
    start: subprocess.CompletedProcess = bulk_fleet_scenario["start"]
    # Act
    rc = start.returncode
    # Assert
    assert rc == 0, (
        f"`sac agents start <fleet_dir>` failed with rc={rc}\n"
        f"stdout:\n{start.stdout}\nstderr:\n{start.stderr}"
    )


def test_first_fleet_agent_appears_in_registry(
    sac_bin: str, bulk_fleet_scenario: dict
) -> None:
    # Arrange
    first = bulk_fleet_scenario["names"][0]
    snap = _registry_snapshot(sac_bin, bulk_fleet_scenario["env"])
    # Act
    present = any(row.get("name") == first for row in snap.get("agents", []))
    # Assert
    assert present, (
        f"after bulk-start, the first fleet agent {first!r} is missing "
        f"from `sac agents list --json`: {snap}"
    )


def test_second_fleet_agent_appears_in_registry(
    sac_bin: str, bulk_fleet_scenario: dict
) -> None:
    # Arrange
    second = bulk_fleet_scenario["names"][1]
    snap = _registry_snapshot(sac_bin, bulk_fleet_scenario["env"])
    # Act
    present = any(row.get("name") == second for row in snap.get("agents", []))
    # Assert
    assert present, (
        f"after bulk-start, the second fleet agent {second!r} is missing "
        f"from `sac agents list --json`: {snap}"
    )


# EOF
