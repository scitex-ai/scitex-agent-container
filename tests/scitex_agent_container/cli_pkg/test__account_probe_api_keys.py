"""Quota probes observe authored rotations instead of stale inherited keys."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_slot_secrets_observe_authored_rotation_over_inherited_key(tmp_path):
    # Arrange
    alias = "COMMANDCODE_API_KEY_ROTATION_TEST"
    missing = "COMMANDCODE_API_KEY_MISSING_TEST"
    source = tmp_path / "pool.src"
    source.write_text(f"export {alias}=full-rotated-test-credential\n")
    env = dict(os.environ, SAC_SECRETS_ENVRC=str(source))
    env[alias] = "stale-truncated-test-credential"
    env.pop(missing, None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    code = (
        "import json; from scitex_agent_container.cli_pkg._account_probe_api_keys "
        f"import _slot_secrets; print(json.dumps(_slot_secrets([{alias!r}, {missing!r}])))"
    )
    # Act
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )
    observed = json.loads(result.stdout) if result.returncode == 0 else result.stderr
    # Assert
    assert (result.returncode, observed) == (
        0, {alias: "full-rotated-test-credential", missing: ""},
    )
