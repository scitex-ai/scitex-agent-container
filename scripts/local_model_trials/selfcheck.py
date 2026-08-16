#!/usr/bin/env python3
"""Pre-flight: materialize every fixture and verify its base state.

S1/S2/M1/L1 base repos must be pytest-green; M2 must be red (planted bug).
Also spot-checks the deletion detector on a synthetic deletion.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import detectors
from fixtures import FIXTURES, load_files
from harness import Trial, _run_pytest


def main() -> int:
    failures = []
    base = tempfile.mkdtemp(prefix="lmt-selfcheck-")
    for rung in FIXTURES:
        outdir = os.path.join(base, rung)
        os.makedirs(outdir)
        trial = Trial("qwen", rung, outdir)  # model never called here
        trial.materialize()
        code, tail = _run_pytest(trial.repo)
        want_red = rung == "M2"
        if want_red and code == 0:
            failures.append(f"{rung}: planted bug does NOT fail tests")
        if not want_red and code != 0:
            failures.append(f"{rung}: base repo tests RED:\n{tail[-500:]}")
        ok, detail = FIXTURES[rung]["assert"](trial.repo)
        if ok:
            failures.append(
                f"{rung}: task assert passes on the BASE repo ({detail}) — "
                "it would reward a no-op")
        print(f"{rung}: base pytest exit={code} "
              f"(expected {'red' if want_red else 'green'}), "
              f"base assert correctly fails: {not ok}", flush=True)

    before = load_files("S1")
    after = dict(before)
    after["calc.py"] = after["calc.py"].replace(
        "def clamp(value, low, high):", "def clamp_renamed(value, low, high):")
    dels = detectors.detect_deletions(before, after)
    if dels["deleted"] != ["calc.py::func:clamp"]:
        failures.append(f"deletion detector missed a deletion: {dels}")
    else:
        print("deletion detector: caught calc.py::func:clamp", flush=True)

    shutil.rmtree(base, ignore_errors=True)
    if failures:
        print("\nSELF-CHECK FAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("self-check OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
