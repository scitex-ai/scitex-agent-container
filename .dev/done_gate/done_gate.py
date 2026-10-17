"""DONE-gate: refuse unevidenced DONE claims (ADR-0032).

A DONE claim is a JSON object; the gate exits 0 (PASS) only when every
applicable criterion carries attached evidence, else exits 1 (REFUSED) and
logs the refusal. Refusal output is itself evidence the gate works.

Required claim shape:
{
  "card": "<card id>",
  "step": "<what is claimed done>",
  "placement": {"host": ..., "workdir": ..., "trigger": ...},
  "path_proof": [{"host": ..., "path": ..., "present": true,
                  "sha256": "<hex>"}],
  "tests": [{"name": ..., "result": "pass", "output_ref": ...}]
}

Rules (from ADR-0032):
- placement: all three fields non-empty (criterion 2).
- path_proof: non-empty list; every entry present=true with a 64-hex
  sha256 (criterion 3 — the compute-02 missing-path failure mode).
- tests: non-empty list; every result == "pass" with an output_ref
  (criteria 1-4 evidence; independence itself is demonstrated by schedule,
  see --check-schedule).
"""

import json
import re
import sys

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def gate_claim(claim):
    """Return (passed: bool, refusals: list[str])."""
    refusals = []
    if not isinstance(claim, dict):
        return False, ["claim is not a JSON object"]

    placement = claim.get("placement") or {}
    for field in ("host", "workdir", "trigger"):
        if not placement.get(field):
            refusals.append(f"placement.{field} missing or empty")

    proofs = claim.get("path_proof")
    if not proofs:
        refusals.append("path_proof empty: no per-host path evidence")
    else:
        for i, p in enumerate(proofs):
            tag = p.get("host", f"entry#{i}")
            if p.get("present") is not True:
                refusals.append(f"path_proof[{tag}]: path absent "
                                f"({p.get('path', '?')})")
            elif not (p.get("sha256") and SHA256.match(p["sha256"])):
                refusals.append(f"path_proof[{tag}]: sha256 content proof "
                                f"missing or malformed")

    tests = claim.get("tests")
    if not tests:
        refusals.append("tests empty: no acceptance-test evidence")
    else:
        for t in tests:
            name = t.get("name", "?")
            if t.get("result") != "pass":
                refusals.append(f"tests[{name}]: result != pass")
            elif not t.get("output_ref"):
                refusals.append(f"tests[{name}]: output_ref missing")

    return (not refusals), refusals


def main(argv):
    if len(argv) != 2:
        print("usage: done_gate.py <claim.json>", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        claim = json.load(f)
    passed, refusals = gate_claim(claim)
    card = claim.get("card", "?") if isinstance(claim, dict) else "?"
    step = claim.get("step", "?") if isinstance(claim, dict) else "?"
    if passed:
        print(f"DONE-GATE PASS card={card} step={step}")
        return 0
    print(f"DONE-GATE REFUSED card={card} step={step}")
    for r in refusals:
        print(f"  - {r}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
