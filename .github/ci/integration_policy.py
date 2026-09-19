#!/usr/bin/env python3
"""Pure policy decisions for the GitHub-backed integration queue."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys

_HIGH_RISK_WORDS = {
    "auth",
    "authentication",
    "authorization",
    "authz",
    "billing",
    "credential",
    "credentials",
    "deploy",
    "deployment",
    "login",
    "iam",
    "migration",
    "migrations",
    "oauth",
    "payment",
    "payments",
    "production",
    "release",
    "rbac",
    "schema",
    "secret",
    "secrets",
    "tenant",
    "tenancy",
    "token",
    "tokens",
}
_HIGH_RISK_STEMS = (
    "authn",
    "authheal",
    "authoriz",
    "cred",
    "deploy",
    "migrat",
    "oauth",
    "releas",
    "schema",
)
_HIGH_RISK_PREFIXES = (
    ".github/workflows/",
    ".github/ci/",
    "policy/",
    "policies/",
    "__global__/",
)
_FLEET_MODES = frozenset({"BUILD", "BALANCED", "INTEGRATION_HEAVY", "DRAIN"})
_WORK_KINDS = frozenset(
    {
        "feature",
        "routine",
        "nice_to_have",
        "operator_p0",
        "security",
        "dependency_unblock",
        "bug_fix",
        "integration",
        "review",
    }
)
_WORK_KIND_ALIASES = {
    "nice": "nice_to_have",
    "operator-P0": "operator_p0",
    "dependency-unblock": "dependency_unblock",
    "bug-fix": "bug_fix",
}
_DRAIN_ALLOWED = frozenset(
    {
        "operator_p0",
        "security",
        "dependency_unblock",
        "bug_fix",
        "integration",
        "review",
    }
)


def fleet_mode(open_green: int) -> str:
    """Compatibility projection using the authorized pressure thresholds."""
    return pressure_mode(integration_pressure(open_green, 0, 0))


def integration_pressure(
    open_green: int | float, open_prs: int | float, p90_minutes: int | float
) -> float:
    """Return max(green/10, open/60, green-to-merge-p90/120)."""
    values = tuple(float(value) for value in (open_green, open_prs, p90_minutes))
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("pressure inputs must be finite non-negative numbers")
    green, opened, p90 = values
    return max(green / 10.0, opened / 60.0, p90 / 120.0)


def pressure_mode(pressure: float) -> str:
    """Map measured pressure to the four capacity modes."""
    measured = float(pressure)
    if not math.isfinite(measured) or measured < 0:
        return "DRAIN"
    if measured < 0.5:
        return "BUILD"
    if measured <= 1.0:
        return "BALANCED"
    if measured <= 2.0:
        return "INTEGRATION_HEAVY"
    return "DRAIN"


def dispatch_allowed(mode: str, work_kind: str) -> bool:
    """Fail closed; DRAIN admits only explicit integration/P0 exceptions."""
    normalized_kind = _WORK_KIND_ALIASES.get(work_kind, work_kind)
    if mode not in _FLEET_MODES or normalized_kind not in _WORK_KINDS:
        return False
    if mode == "DRAIN":
        return normalized_kind in _DRAIN_ALLOWED
    return True


def changed_paths_from_pages(expected_count: int, pages: list[list[dict]]) -> list[str]:
    """Flatten GitHub files pages only when all ``changed_files`` arrived."""
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise ValueError("changed-files expected count is invalid")
    files = [item for page in pages for item in page]
    if len(files) != expected_count:
        raise ValueError(
            f"changed-files API was truncated: expected {expected_count}, got {len(files)}"
        )
    paths: list[str] = []
    for item in files:
        filename = str(item.get("filename") or "").strip()
        previous = str(item.get("previous_filename") or "").strip()
        if filename:
            paths.append(filename)
        if previous:
            paths.append(previous)
    return paths


def complete_search_count(payload: dict) -> int:
    """Return total_count only for a complete GitHub Search response."""
    if payload.get("incomplete_results") is not False:
        raise ValueError("GitHub Search result is incomplete")
    count = payload.get("total_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("GitHub Search total_count is invalid")
    return count


def check_counts(payload: dict) -> tuple[int, int, int]:
    """Classify one atomic check-runs response as failing/busy/total."""
    runs = payload.get("check_runs")
    if not isinstance(runs, list):
        raise ValueError("check-runs payload is invalid")
    reported_total = payload.get("total_count")
    if (
        isinstance(reported_total, bool)
        or not isinstance(reported_total, int)
        or reported_total != len(runs)
    ):
        raise ValueError("check-runs snapshot is truncated or missing total_count")
    included = [
        run
        for run in runs
        if not re.search("codecov|readthedocs", str(run.get("name") or ""), re.I)
    ]
    failing = sum(
        str(run.get("status") or "") == "completed"
        and str(run.get("conclusion") or "")
        not in {"success", "neutral", "skipped"}
        for run in included
    )
    busy = sum(str(run.get("status") or "") != "completed" for run in included)
    return failing, busy, len(included)


def rollup_counts(
    payload: dict, *, expected_head: str | None = None
) -> tuple[int, int, int, int]:
    """Classify a PR statusCheckRollup as pass/fail/unknown/total."""
    if expected_head is not None and payload.get("headRefOid") != expected_head:
        raise ValueError("statusCheckRollup head does not match expected head")
    rollup = payload.get("statusCheckRollup")
    if not isinstance(rollup, list):
        raise ValueError("statusCheckRollup payload is invalid")
    passing = failing = unknown = 0
    for item in rollup:
        name = str(item.get("name") or item.get("context") or "")
        if re.search("codecov|readthedocs", name, re.I):
            continue
        status = str(item.get("status") or "").upper()
        verdict = str(item.get("conclusion") or item.get("state") or "").upper()
        if status not in {"", "COMPLETED"}:
            unknown += 1
        elif verdict in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            passing += 1
        elif verdict in {
            "FAILURE",
            "ERROR",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
        }:
            failing += 1
        else:
            unknown += 1
    return passing, failing, unknown, passing + failing + unknown


def missing_protected_contexts(
    payload: dict, required: tuple[str, ...]
) -> list[str]:
    """Require strict develop protection containing every workflow context."""
    if payload.get("strict") is not True:
        raise ValueError("required status protection is not strict")
    contexts = payload.get("contexts")
    checks = payload.get("checks", [])
    if not isinstance(contexts, list) or not isinstance(checks, list):
        raise ValueError("required status protection payload is invalid")
    protected = {str(context) for context in contexts}
    protected.update(
        str(check.get("context") or "") for check in checks if isinstance(check, dict)
    )
    return [name for name in required if name not in protected]


def missing_required_contexts(payload: dict, required: tuple[str, ...]) -> list[str]:
    """Return required context names lacking an exact SUCCESS verdict."""
    rollup = payload.get("statusCheckRollup")
    if not isinstance(rollup, list):
        raise ValueError("statusCheckRollup payload is invalid")
    verdicts: dict[str, list[bool]] = {}
    for item in rollup:
        name = str(item.get("name") or item.get("context") or "")
        status = str(item.get("status") or "").upper()
        verdict = str(item.get("conclusion") or item.get("state") or "").upper()
        if name:
            verdicts.setdefault(name, []).append(
                status in {"", "COMPLETED"} and verdict == "SUCCESS"
            )
    return [
        name
        for name in required
        if verdicts.get(name) != [True]
    ]


def _normalize_path(path: str) -> str:
    """Drop only explicit relative prefixes; preserve ``.github`` identity."""
    normalized = path.lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def classify_risk(changed_paths: list[str]) -> str:
    """Classify a PR from the paths GitHub reports as changed."""
    if not changed_paths:
        return "HIGH"

    for path in changed_paths:
        low = _normalize_path(path)
        if low.startswith(_HIGH_RISK_PREFIXES):
            return "HIGH"
        words = set(re.split(r"[/_.-]+", low))
        documentation_or_test = low.startswith(("docs/", "tests/"))
        sensitive = bool(words & _HIGH_RISK_WORDS) or any(
            word.startswith(stem) for word in words for stem in _HIGH_RISK_STEMS
        )
        if sensitive and not documentation_or_test:
            return "HIGH"

    low_risk = all(
        _normalize_path(path).startswith(("docs/", "tests/", "github/"))
        or "/" not in path
        and path.lower().endswith(".md")
        for path in changed_paths
    )
    return "LOW" if low_risk else "NORMAL"


def review_verdict(head_sha: str, reviews: list[dict]) -> str:
    """Fold formal GitHub reviews for one exact PR head."""
    exact = [review for review in reviews if review.get("commit_id") == head_sha]
    exact.sort(key=lambda review: (review.get("submitted_at") or "", review.get("id") or 0))
    latest_by_reviewer: dict[str, str] = {}
    for index, review in enumerate(exact):
        user = review.get("user") or {}
        reviewer = str(user.get("login") or f"__unknown_{index}")
        state = str(review.get("state", "")).upper()
        if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest_by_reviewer[reviewer] = state
    states = set(latest_by_reviewer.values())
    if "CHANGES_REQUESTED" in states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in states:
        return "APPROVED"
    return "WAITING"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    mode_parser = subparsers.add_parser("mode")
    mode_parser.add_argument("open_green")
    mode_parser.add_argument("open_prs")
    mode_parser.add_argument("p90_minutes")
    subparsers.add_parser("risk")
    paths_parser = subparsers.add_parser("paths")
    paths_parser.add_argument("expected_count", type=int)
    subparsers.add_parser("search-count")
    subparsers.add_parser("check-counts")
    rollup_parser = subparsers.add_parser("rollup-counts")
    rollup_parser.add_argument("--expected-head")
    protection_parser = subparsers.add_parser("protection")
    protection_parser.add_argument("contexts", nargs="+")
    required_parser = subparsers.add_parser("required-contexts")
    required_parser.add_argument("contexts", nargs="+")
    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("head_sha")
    admit_parser = subparsers.add_parser("admit")
    admit_parser.add_argument("--open-green", required=True)
    admit_parser.add_argument("--open-prs", required=True)
    admit_parser.add_argument("--p90-minutes", required=True)
    admit_parser.add_argument("--work-kind", required=True)

    args = parser.parse_args()

    if args.command == "mode":
        print(
            pressure_mode(
                integration_pressure(
                    args.open_green, args.open_prs, args.p90_minutes
                )
            )
        )
    elif args.command == "risk":
        print(classify_risk([line.rstrip("\n") for line in sys.stdin if line.strip()]))
    elif args.command == "paths":
        pages = json.load(sys.stdin)
        for path in changed_paths_from_pages(args.expected_count, pages):
            print(path)
    elif args.command == "search-count":
        print(complete_search_count(json.load(sys.stdin)))
    elif args.command == "check-counts":
        print(*check_counts(json.load(sys.stdin)))
    elif args.command == "rollup-counts":
        print(
            *rollup_counts(
                json.load(sys.stdin), expected_head=args.expected_head
            )
        )
    elif args.command == "protection":
        missing = missing_protected_contexts(json.load(sys.stdin), tuple(args.contexts))
        if missing:
            print("MISSING " + " ".join(missing))
            return 2
        print("OK")
    elif args.command == "required-contexts":
        missing = missing_required_contexts(json.load(sys.stdin), tuple(args.contexts))
        if missing:
            print("MISSING " + " ".join(missing))
            return 2
        print("OK")
    elif args.command == "review":
        payload = json.load(sys.stdin)
        reviews = [review for page in payload for review in page]
        print(review_verdict(args.head_sha, reviews))
    elif args.command == "admit":
        try:
            mode = pressure_mode(
                integration_pressure(
                    args.open_green, args.open_prs, args.p90_minutes
                )
            )
        except (TypeError, ValueError):
            print("REJECT DRAIN")
            return 2
        allowed = dispatch_allowed(mode, args.work_kind)
        print(f"{'ALLOW' if allowed else 'REJECT'} {mode}")
        return 0 if allowed else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
