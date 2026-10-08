"""DONE-gate acceptance tests (ADR-0032 criteria 1-4).

Each test ties to the reproduced failure mode it guards.
Run: python3 -m pytest test_done_gate.py -v
"""

import json
import subprocess
import sys

import pytest

from done_gate import gate_claim

GATE = "done_gate.py"

# The PG18 .claim-only failure mode: DONE asserted, zero evidence.
CLAIM_ONLY = {"card": "infra-ci-critical-reliability-20261008",
              "step": "pg18 org-var path restored on 02"}

# The 02 failure mode: path reported absent on one host.
MISSING_PATH = {
    "card": "infra-ci-critical-reliability-20261008",
    "step": "pg18 org-var path restored on 02",
    "placement": {"host": "scitex-compute-02",
                  "workdir": "/home/ywatanabe/proj/scitex-agent-container",
                  "trigger": "manual incident response"},
    "path_proof": [
        {"host": "scitex-compute-02", "path": "/org/var/pg18/primary",
         "present": False, "sha256": None},
    ],
    "tests": [],
}

EVIDENCED = {
    "card": "infra-ci-critical-reliability-20261008",
    "step": "pg18 org-var path restored on 02",
    "placement": {"host": "scitex-compute-02",
                  "workdir": "/home/ywatanabe/proj/scitex-agent-container",
                  "trigger": "manual incident response"},
    "path_proof": [
        {"host": "scitex-compute-02", "path": "/org/var/pg18/primary",
         "present": True, "sha256": "e3b0c44298fc1c149afbf4c8996fb924"
         "27ae41e4649b934ca495991b7852b855"},
    ],
    "tests": [{"name": "pg18-path-present-02", "result": "pass",
               "output_ref": "ci-run:37721924961"},
              {"name": "sha256-match-before-after-mv", "result": "pass",
               "output_ref": "ci-run:37721924961"}],
}


def run_gate(claim, tmp_path):
    p = tmp_path / "claim.json"
    p.write_text(json.dumps(claim))
    return subprocess.run([sys.executable, GATE, str(p)],
                          capture_output=True, text=True)


def test_1_claim_only_done_is_refused(tmp_path):
    """Criterion 4: assertion without evidence cannot pass."""
    proc = run_gate(CLAIM_ONLY, tmp_path)
    assert proc.returncode == 1
    assert "REFUSED" in proc.stdout


def test_2_missing_placement_is_refused():
    """Criterion 2: unplaced execution is refused."""
    claim = dict(EVIDENCED, placement={"host": "scitex-compute-02"})
    passed, refusals = gate_claim(claim)
    assert not passed
    assert any("placement." in r for r in refusals)


def test_3_absent_path_with_no_hash_is_refused():
    """Criterion 3: the 02 missing-path mode is refused, loudly."""
    passed, refusals = gate_claim(MISSING_PATH)
    assert not passed
    assert any("absent" in r for r in refusals)


def test_4_evidenced_claim_passes():
    """Gate is not a brick wall: full evidence passes."""
    passed, refusals = gate_claim(EVIDENCED)
    assert passed, refusals


def test_4_refusal_is_itself_logged_evidence(tmp_path):
    """Criterion 4: the refusal names every missing item."""
    proc = run_gate(CLAIM_ONLY, tmp_path)
    assert "placement.host" in proc.stdout
    assert "path_proof" in proc.stdout
    assert "tests" in proc.stdout


def test_non_object_claim_is_refused():
    passed, _ = gate_claim(["not", "a", "claim"])
    assert not passed


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
