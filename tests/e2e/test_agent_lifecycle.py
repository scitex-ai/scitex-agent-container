"""E2E: full ``sac agents start … list … stop`` lifecycle.

What this test covers
=====================
Drives the real ``sac`` binary through an agent's full lifecycle
against the actual apptainer runtime and the actual registry DB. No
mocks; no monkeypatching of internal modules.

Workflow
--------
1. Materialise a throwaway agent spec under ``tmp_home_with_image``
   that points at the real ``sac-base.sif`` image.
2. ``sac agents start <name>`` — real subprocess, real apptainer.
3. Poll ``sac agents list --json`` until the registry row reports
   ``running``.
4. ``sac agents stop <name>`` — verify the row transitions away from
   ``running``.

Skip strategy
-------------
* Module-level ``pytest.mark.e2e``.
* ``RUN_E2E`` env gate from conftest.
* Skipif when ``apptainer`` binary or base image is unavailable.

Each test owns its cleanup via a ``finally`` that issues ``sac agents
stop --force`` so a failing assert does not leave a container behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.e2e.conftest import wait_for_status

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("RUN_E2E"), reason="E2E disabled by default"),
    pytest.mark.skipif(
        shutil.which("apptainer") is None,
        reason="apptainer binary not on PATH",
    ),
]


# ---------------------------------------------------------------------------
# Helpers — materialise a minimal v3 agent spec under a tmp_home tree.
# ---------------------------------------------------------------------------


def _write_minimal_spec(home: Path, name: str) -> Path:
    """Drop a minimal apptainer-runtime agent spec.yaml under ``home``."""
    agent_dir = home / ".scitex/agent-container/agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        textwrap.dedent(
            f"""\
            apiVersion: scitex-agent-container/v3
            kind: Agent
            metadata:
              labels:
                role: e2e-test
                description: ephemeral lifecycle smoke target
            spec:
              runtime: apptainer
              host: local
              workdir: {home}/work
              apptainer:
                image: {home}/.scitex/agent-container/containers/sac-base.sif
                binds: []
              claude:
                model: haiku
                flags:
                  - --dangerously-skip-permissions
                session: new-session
              health:
                enabled: true
                interval: 60
              restart:
                policy: on-failure
                max_retries: 3
            """
        )
    )
    return spec


@pytest.fixture
def lifecycle_scenario(
    sac_bin: str,
    apptainer_available: bool,
    tmp_home_with_image: Path,
    unique_agent_name: str,
):
    """Materialise a spec, start the agent, yield the live name; stop on teardown."""
    if not apptainer_available:
        pytest.skip("apptainer + sac-base.sif unavailable on this host")

    spec = _write_minimal_spec(tmp_home_with_image, unique_agent_name)
    env = {**os.environ, "HOME": str(tmp_home_with_image)}

    start_proc = subprocess.run(
        [sac_bin, "agents", "start", str(spec), "--no-preflight", "-y"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    try:
        yield {
            "name": unique_agent_name,
            "spec": spec,
            "start": start_proc,
            "env": env,
        }
    finally:
        subprocess.run(
            [sac_bin, "agents", "stop", unique_agent_name, "--force", "-y"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )


# ---------------------------------------------------------------------------
# Assertions — one observable per test.
# ---------------------------------------------------------------------------


def test_agent_start_command_exits_successfully(lifecycle_scenario: dict) -> None:
    # Arrange
    start: subprocess.CompletedProcess = lifecycle_scenario["start"]
    # Act
    rc = start.returncode
    # Assert
    assert rc == 0, (
        f"`sac agents start` failed with rc={rc}\n"
        f"stdout:\n{start.stdout}\nstderr:\n{start.stderr}"
    )


def test_started_agent_reaches_running_state_in_registry(
    sac_bin: str, lifecycle_scenario: dict
) -> None:
    # Arrange
    name = lifecycle_scenario["name"]
    # Act
    observed = wait_for_status(sac_bin, name, "running", timeout=90)
    # Assert
    assert observed == "running", (
        f"agent {name!r} never reached `running` in the registry; "
        f"last observed status={observed!r}"
    )


def test_stop_transitions_agent_out_of_running_state(
    sac_bin: str, lifecycle_scenario: dict
) -> None:
    # Arrange
    name = lifecycle_scenario["name"]
    env = lifecycle_scenario["env"]
    wait_for_status(sac_bin, name, "running", timeout=90)
    # Act
    subprocess.run(
        [sac_bin, "agents", "stop", name, "-y"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    final = wait_for_status(sac_bin, name, "stopped", timeout=60)
    # Assert
    assert final != "running", (
        f"after `sac agents stop`, registry still reports running; "
        f"final status={final!r}"
    )


# EOF
