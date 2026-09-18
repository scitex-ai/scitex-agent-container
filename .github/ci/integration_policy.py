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
    """Return the deterministic fleet mode for an open-green backlog."""
    if open_green > 40:
        return "DRAIN"
    if open_green > 20:
        return "INTEGRATION_HEAVY"
    if open_green > 10:
        return "BALANCED"
    return "BUILD"


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
    mode_parser.add_argument("metrics", nargs="+")
    subparsers.add_parser("risk")
    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("head_sha")
    admit_parser = subparsers.add_parser("admit")
    admit_parser.add_argument("--open-green", required=True)
    admit_parser.add_argument("--open-prs", required=True)
    admit_parser.add_argument("--p90-minutes", required=True)
    admit_parser.add_argument("--work-kind", required=True)

    args = parser.parse_args()

    if args.command == "mode":
        if len(args.metrics) == 1:
            print(fleet_mode(int(args.metrics[0])))
        elif len(args.metrics) == 3:
            print(
                pressure_mode(
                    integration_pressure(
                        args.metrics[0], args.metrics[1], args.metrics[2]
                    )
                )
            )
        else:
            parser.error("mode requires open_green or open_green open_prs p90_minutes")
    elif args.command == "risk":
        print(classify_risk([line.rstrip("\n") for line in sys.stdin if line.strip()]))
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
