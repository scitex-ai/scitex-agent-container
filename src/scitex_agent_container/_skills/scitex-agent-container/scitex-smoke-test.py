#!/usr/bin/env python3
"""Smoke test for scitex ecosystem packages.

For each package, attempts:
  1. `import <module>`
  2. CLI `<entry> --help`

Packages not installed are reported as SKIP. Exit 0 if no FAIL, else 1.

Fleet regression prevention script. Validated across the Orochi fleet
(Spartan, NAS, MBA, ywata-note-win) prior to landing here. Lives beside
the scitex-agent-container skill so agents and CI can discover it.

Tracks: ywatanabe1989/todo#194
Origin: authored by head-spartan in the Orochi fleet.
Usage:
    ./scitex-smoke-test.py            # human-readable
    ./scitex-smoke-test.py --json     # machine-readable
"""
import argparse
import importlib
import json
import shutil
import subprocess
import sys

# (pip-name, import-module, cli-entry)
PACKAGES = [
    ("scitex",                  "scitex",                  "scitex"),
    ("scitex-agent-container",  "scitex_agent_container",  "scitex-agent-container"),
    ("scitex-container",        "scitex_container",        "scitex-container"),
    ("scitex-dev",              "scitex_dev",              "scitex-dev"),
    ("scitex-orochi",           "scitex_orochi",           "scitex-orochi"),
    ("scitex-ui",               "scitex_ui",               "scitex-ui"),
    ("scitex-io",               "scitex_io",               "scitex-io"),
    ("scitex-plt",              "scitex_plt",              "scitex-plt"),
    ("scitex-stats",            "scitex_stats",            "scitex-stats"),
    ("scitex-pd",               "scitex_pd",               "scitex-pd"),
    ("scitex-str",              "scitex_str",              "scitex-str"),
    ("scitex-path",             "scitex_path",             "scitex-path"),
    ("scitex-dict",             "scitex_dict",             "scitex-dict"),
    ("scitex-gen",              "scitex_gen",              "scitex-gen"),
    ("scitex-dsp",              "scitex_dsp",              "scitex-dsp"),
    ("scitex-torch",            "scitex_torch",            "scitex-torch"),
]


def test_import(module):
    try:
        importlib.import_module(module)
        return True, ""
    except ImportError:  # stx-allow: fallback (reason: optional dependency not installed)
        return None, "not installed"
    except Exception as e:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return False, f"{type(e).__name__}: {e}"


def test_cli(entry, timeout=15):
    path = shutil.which(entry)
    if not path:
        return None, "cli not found"
    try:
        r = subprocess.run([path, "--help"], capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode == 0:
            return True, ""
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        return False, f"exit {r.returncode}: {tail[0][:160]}"
    except subprocess.TimeoutExpired:  # stx-allow: fallback (reason: subprocess execution failure)
        return False, f"timeout after {timeout}s"
    except Exception as e:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return False, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    results = []
    for pip_name, module, entry in PACKAGES:
        imp_ok, imp_msg = test_import(module)
        if imp_ok is None:
            status, detail = "SKIP", imp_msg
        else:
            cli_ok, cli_msg = test_cli(entry)
            if imp_ok and (cli_ok or cli_ok is None):
                status = "PASS"
                detail = cli_msg if cli_ok is None else ""
            else:
                status = "FAIL"
                detail = "; ".join(x for x in [
                    f"import: {imp_msg}" if not imp_ok else "",
                    f"cli: {cli_msg}" if cli_ok is False else "",
                ] if x)
        results.append({"package": pip_name, "status": status, "detail": detail})

    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        counts[r["status"]] += 1

    if args.json:
        print(json.dumps({"results": results, "summary": counts}, indent=2))
    else:
        width = max(len(r["package"]) for r in results)
        for r in results:
            line = f"  {r['status']:4}  {r['package']:<{width}}"
            if r["detail"]:
                line += f"  ({r['detail']})"
            print(line)
        print(f"\nSummary: {counts['PASS']} pass, {counts['FAIL']} fail, "
              f"{counts['SKIP']} skip (of {len(results)})")

    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
