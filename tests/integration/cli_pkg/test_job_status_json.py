"""Real SAC -> installed Dev status, with no host supervision binaries available."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_timer_json_roundtrip_uses_actual_owner_status_without_host_supervisors(tmp_path):
    # Arrange
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "PATH": str(empty_path),
        "SCITEX_DIR": str(tmp_path / "state"),
        "LANG": "C.UTF-8",
    }
    source_root = Path(__file__).resolve().parents[3]
    # Act
    result = subprocess.run(
        [sys.executable, "-m", "scitex_agent_container.cli", "dev", "timer",
         "status", "accounts-refresh", "--json"],
        cwd=source_root, env=environment, capture_output=True, text=True,
        timeout=30, check=False,
    )
    observation = (result.returncode, result.stderr, json.loads(result.stdout))
    # Assert
    assert observation == (0, "", {
        "name": "scitex-agent-container-accounts-refresh", "kind": "timer",
        "host_mechanism": "none", "host_supports_kind": False,
        "installed_units": [], "active_state": None, "supervisors": [],
    })
