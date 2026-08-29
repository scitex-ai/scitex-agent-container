#!/usr/bin/env python3
"""Run the task ladder for one model, or aggregate finished results.

Usage:
  run_ladder.py run --model qwen --reps 3 --out DIR [--rungs S1,S2,...]
  run_ladder.py aggregate --out DIR [DIR2 ...]

Each trial lands in DIR/<model>-<rung>-rep<k>/ with repo/, transcript.json
and result.json. Aggregation is a pure re-read of result.json files, so it
can run any time, over partial results, and over multiple result dirs.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

from fixtures import FIXTURES
from harness import MODELS, Trial

RUNG_ORDER = ["S1", "S2", "M1", "M2", "L1", "L2"]


def cmd_run(args) -> int:
    rungs = args.rungs.split(",") if args.rungs else RUNG_ORDER
    for rung in rungs:
        if rung not in FIXTURES:
            sys.exit(f"unknown rung: {rung}")
    os.makedirs(args.out, exist_ok=True)
    for rung in rungs:
        for rep in range(1, args.reps + 1):
            outdir = os.path.join(args.out, f"{args.model}-{rung}-rep{rep}")
            if os.path.exists(os.path.join(outdir, "result.json")):
                print(f"skip {outdir} (already done)", flush=True)
                continue
            os.makedirs(outdir, exist_ok=True)
            trial = Trial(args.model, rung, outdir)
            result = trial.run()
            verdict = "PASS" if result["passed"] else (
                "FAIL:" + ",".join(result["failure_modes"]))
            print(f"{args.model} {rung} rep{rep}: {verdict} "
                  f"({result['seconds']}s, "
                  f"api_calls={result['stats']['api_calls']})", flush=True)
    return 0


def cmd_aggregate(args) -> int:
    rows = []
    for out in args.out:
        for path in sorted(glob.glob(os.path.join(out, "*", "result.json"))):
            with open(path, encoding="utf-8") as fh:
                rows.append(json.load(fh))
    if not rows:
        sys.exit("no result.json found")
    table = {}
    for r in rows:
        key = (r["model"], r["rung"])
        cell = table.setdefault(key, {
            "pass": 0, "n": 0, "modes": Counter(), "dishonest": 0})
        cell["n"] += 1
        cell["pass"] += bool(r["passed"])
        for m in r.get("failure_modes", []):
            cell["modes"][m] += 1
        honesty = r.get("honesty") or {}
        if honesty and not honesty.get("honest", True):
            cell["dishonest"] += 1
    models = sorted({m for m, _ in table})
    print(f"{'rung':<5}", end="")
    for m in models:
        print(f"{m:>34}", end="")
    print()
    for rung in RUNG_ORDER:
        line = f"{rung:<5}"
        for m in models:
            cell = table.get((m, rung))
            if not cell:
                line += f"{'-':>34}"
                continue
            modes = ",".join(f"{k}x{v}" for k, v in
                             cell["modes"].most_common(2)) or "-"
            line += f"{cell['pass']}/{cell['n']} {modes:>28}"[:34].rjust(34)
        print(line)
    print("\nhonesty deltas (summary omitted/misstated real changes):")
    for (m, rung), cell in sorted(table.items()):
        if cell["dishonest"]:
            print(f"  {m} {rung}: {cell['dishonest']}/{cell['n']} trials")
    print("\nper-trial detail:")
    for r in sorted(rows, key=lambda r: (r["model"],
                                         RUNG_ORDER.index(r["rung"]))):
        modes = ",".join(r.get("failure_modes", [])) or "PASS"
        honesty = r.get("honesty") or {}
        flag = "" if honesty.get("honest", True) else "  DISHONEST-SUMMARY"
        print(f"  {r['model']:<8} {r['rung']}  {modes}{flag}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--model", required=True, choices=sorted(MODELS))
    p_run.add_argument("--reps", type=int, default=3)
    p_run.add_argument("--rungs", default="")
    p_run.add_argument("--out", required=True)
    p_agg = sub.add_parser("aggregate")
    p_agg.add_argument("--out", nargs="+", required=True)
    args = parser.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_aggregate(args)


if __name__ == "__main__":
    sys.exit(main())
