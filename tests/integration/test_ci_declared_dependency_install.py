"""A failed full release install must never reach reduced extras or tests."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/ci/run-in-sif.sh"


@pytest.mark.parametrize("full_status", (0, 37))
def test_declared_full_install_is_one_fatal_attempt(tmp_path, full_status):
    # Arrange: real shell and executable witnesses, not resolver qualification.
    source = SCRIPT.read_text()
    start = source.index('# Install scitex-agent-container + its [all,dev] extras')
    end = source.index('# The CI SIF ships no SYSTEM tzdata', start)
    install = source[start:end]
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "calls.jsonl"
    witness = binary / "uv"
    witness.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        'with open(os.environ["OWNED_CALLS"], "a") as stream:\n'
        '    stream.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'full = ".[all,dev]" in sys.argv\n'
        'sys.exit(int(os.environ["OWNED_FULL_STATUS"]) if full else 0)\n'
    )
    witness.chmod(0o700)
    environment = {
        "PATH": str(binary), "VENV": str(tmp_path / "venv"),
        "TMPDIR": str(tmp_path), "OWNED_CALLS": str(calls),
        "OWNED_FULL_STATUS": str(full_status),
    }
    # Act: the marker represents the subsequent test/qualification stage.
    result = subprocess.run(
        ["/bin/bash", "-ec", install + "\nprintf subsequent-stage"],
        env=environment, capture_output=True, text=True, timeout=5,
    )
    observed = [json.loads(line) for line in calls.read_text().splitlines()]
    # Assert: an install failure stays a failure, with no weaker second attempt.
    assert (result.returncode, len(observed), observed[0][-1], result.stdout) == (
        full_status, 1, ".[all,dev]", "subsequent-stage" if full_status == 0 else "",
    )
