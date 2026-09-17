#!/usr/bin/env python3
"""Pure policy decisions for the GitHub-backed integration queue."""

from __future__ import annotations

import argparse
import json
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
    "migration",
    "migrations",
    "oauth",
    "secret",
    "secrets",
    "tenant",
    "tenancy",
    "token",
    "tokens",
}


def fleet_mode(open_green: int) -> str:
    """Return the deterministic fleet mode for an open-green backlog."""
    if open_green > 40:
        return "DRAIN"
    if open_green > 20:
        return "INTEGRATION_HEAVY"
    if open_green > 10:
        return "BALANCED"
    return "BUILD"


def dispatch_allowed(mode: str, work_kind: str) -> bool:
    """Admit only drain work after the integration backlog crosses 20."""
    if mode not in {"INTEGRATION_HEAVY", "DRAIN"}:
        return True
    return work_kind in {"critical_fix", "review", "integration"}


def classify_risk(changed_paths: list[str]) -> str:
    """Classify a PR from the paths GitHub reports as changed."""
    if not changed_paths:
        return "HIGH"

    for path in changed_paths:
        low = path.lower().lstrip("./")
        words = set(re.split(r"[/_.-]+", low))
        documentation_or_test = low.startswith(("docs/", "tests/"))
        if words & _HIGH_RISK_WORDS and not documentation_or_test:
            return "HIGH"

    low_risk = all(
        path.lower().lstrip("./").startswith(("docs/", "tests/", "github/"))
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
        latest_by_reviewer[reviewer] = str(review.get("state", "")).upper()
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
    mode_parser.add_argument("open_green", type=int)
    subparsers.add_parser("risk")
    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("head_sha")
    admit_parser = subparsers.add_parser("admit")
    admit_parser.add_argument("mode")
    admit_parser.add_argument("work_kind")
    args = parser.parse_args()

    if args.command == "mode":
        print(fleet_mode(args.open_green))
    elif args.command == "risk":
        print(classify_risk([line.rstrip("\n") for line in sys.stdin if line.strip()]))
    elif args.command == "review":
        payload = json.load(sys.stdin)
        reviews = [review for page in payload for review in page]
        print(review_verdict(args.head_sha, reviews))
    elif args.command == "admit":
        allowed = dispatch_allowed(args.mode, args.work_kind)
        print("ALLOW" if allowed else "REJECT")
        return 0 if allowed else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
