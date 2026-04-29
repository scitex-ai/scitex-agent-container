#!/usr/bin/env python3
"""Smoke test for scitex ecosystem packages.

For each package, attempts:
  1. `import <module>`
  2. CLI `<entry> --help`
  3. CLI `<entry> --version`  (if _HAS_VERSION flag is set)
  4. CLI `<entry> --help-recursive`  (if _HAS_HELP_RECURSIVE flag is set)

Packages not installed are reported as SKIP. Exit 0 if no FAIL, else 1.

Fleet regression prevention script. Validated across the Orochi fleet
(Spartan, NAS, MBA, ywata-note-win) prior to landing here. Lives beside
the scitex-agent-container skill so agents and CI can discover it.

Tracks: ywatanabe1989/todo#194 (initial), ywatanabe1989/todo#439 (feature parity)
Origin: authored by head-spartan in the Orochi fleet.
Usage:
    ./scitex-smoke-test.py                    # human-readable table
    ./scitex-smoke-test.py --json             # machine-readable JSON
    ./scitex-smoke-test.py --package scitex   # single-package check
"""
import argparse
import importlib
import json
import shutil
import subprocess
import sys

# (pip-name, import-module, cli-entry, has_version, has_help_recursive)
PACKAGES = [
    # Core + container management
    ("scitex",                 "scitex",                 "scitex",                 True,  False),
    ("scitex-agent-container", "scitex_agent_container", "scitex-agent-container", True,  True),
    ("scitex-container",       "scitex_container",       "scitex-container",       True,  False),
    # Developer toolchain
    ("scitex-dev",             "scitex_dev",             "scitex-dev",             True,  False),
    # Hub / communication
    ("scitex-orochi",          "scitex_orochi",          "scitex-orochi",          True,  False),
    # UI components
    ("scitex-ui",              "scitex_ui",              "scitex-ui",              True,  False),
    # Data / analysis libraries
    ("scitex-io",              "scitex_io",              "scitex-io",              True,  False),
    ("scitex-plt",             "scitex_plt",             "scitex-plt",             True,  False),
    ("scitex-stats",           "scitex_stats",           "scitex-stats",           True,  False),
    ("scitex-pd",              "scitex_pd",              "scitex-pd",              True,  False),
    ("scitex-str",             "scitex_str",             "scitex-str",             True,  False),
    ("scitex-path",            "scitex_path",            "scitex-path",            True,  False),
    ("scitex-dict",            "scitex_dict",            "scitex-dict",            True,  False),
    ("scitex-gen",             "scitex_gen",             "scitex-gen",             True,  False),
    ("scitex-dsp",             "scitex_dsp",             "scitex-dsp",             True,  False),
    ("scitex-torch",           "scitex_torch",           "scitex-torch",           True,  False),
    # Domain / HPC
    ("scitex-slurm",           "scitex_slurm",           "scitex-slurm",           True,  False),
    ("scitex-resource",        "scitex_resource",        "scitex-resource",        True,  False),
    # Research writing / tracking
    ("scitex-grant",           "scitex_grant",           "scitex-grant",           True,  False),
    ("scitex-clew",            "scitex_clew",            "scitex-clew",            True,  False),
]


def test_import(module):
    try:
        importlib.import_module(module)
        return True, ""
    except ImportError:
        return None, "not installed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def test_cli(entry, args, timeout=15):
    """Run `entry <args>` and return (ok, message).

    Returns None (SKIP) when the entry point is not on PATH.
    """
    path = shutil.which(entry)
    if not path:
        return None, "cli not found"
    try:
        r = subprocess.run(
            [path] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode == 0:
            return True, ""
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        return False, f"exit {r.returncode}: {tail[0][:160]}"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_package(pip_name, module, entry, has_version, has_help_recursive):
    """Run all checks for one package. Returns a dict with per-check results."""
    checks = {}

    # 1. Import
    imp_ok, imp_msg = test_import(module)
    checks["import"] = {"ok": imp_ok, "msg": imp_msg}

    if imp_ok is None:
        # Package not installed — skip all CLI checks
        return {"package": pip_name, "status": "SKIP", "detail": imp_msg, "checks": checks}

    if not imp_ok:
        checks["help"] = {"ok": None, "msg": "skipped (import failed)"}
        return {"package": pip_name, "status": "FAIL",
                "detail": f"import: {imp_msg}", "checks": checks}

    # 2. --help
    help_ok, help_msg = test_cli(entry, ["--help"])
    checks["help"] = {"ok": help_ok, "msg": help_msg}

    # 3. --version (optional)
    if has_version:
        ver_ok, ver_msg = test_cli(entry, ["--version"])
        checks["version"] = {"ok": ver_ok, "msg": ver_msg}

    # 4. --help-recursive (optional, only for packages that advertise it)
    if has_help_recursive:
        hr_ok, hr_msg = test_cli(entry, ["--help-recursive"])
        checks["help_recursive"] = {"ok": hr_ok, "msg": hr_msg}

    # Aggregate
    failures = []
    for check_name, result in checks.items():
        if result["ok"] is False:
            failures.append(f"{check_name}: {result['msg']}")

    if failures:
        status = "FAIL"
        detail = "; ".join(failures)
    else:
        status = "PASS"
        # Surface any SKIP sub-checks as informational detail
        skips = [n for n, r in checks.items() if r["ok"] is None and n != "import"]
        detail = ", ".join(f"{s}: not found" for s in skips) if skips else ""

    return {"package": pip_name, "status": status, "detail": detail, "checks": checks}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="machine-readable JSON output")
    ap.add_argument("--package", metavar="NAME",
                    help="test only this package (pip name or prefix)")
    args = ap.parse_args()

    targets = PACKAGES
    if args.package:
        targets = [p for p in PACKAGES if p[0] == args.package or p[0].startswith(args.package)]
        if not targets:
            print(f"No package matching {args.package!r}", file=sys.stderr)
            sys.exit(2)

    results = [check_package(*row) for row in targets]

    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        counts[r["status"]] += 1

    if args.as_json:
        print(json.dumps({"results": results, "summary": counts}, indent=2))
    else:
        width = max(len(r["package"]) for r in results)
        for r in results:
            line = f"  {r['status']:4}  {r['package']:<{width}}"
            if r["detail"]:
                line += f"  ({r['detail']})"
            print(line)
        print(
            f"\nSummary: {counts['PASS']} pass, {counts['FAIL']} fail, "
            f"{counts['SKIP']} skip (of {len(results)})"
        )

    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
