#!/usr/bin/env python3
"""Where is CI right now — per COMMIT SHA, not "the latest result".

WHY THIS EXISTS

Nothing in this fleet notifies anyone when CI stalls. The only way to know is
to look, so looking has to be cheap, repeatable and unambiguous. Operator,
2026-08-11: 「何がボトルネックでどこで止まっているのか、どこでちゃんと動いて
いるのかっていうのを逐一うるさい位確認してください」.

AND IT MUST BE ATTRIBUTED TO A SHA. A PR was read as green tonight when it was
not, and the operator named the cause exactly: 「コミットのIDとそれに対する CI
とその結果というのが結びついていないので、最後の結果を見てしまう」 — the
result was not tied to a commit, so the newest row won. That is a real hazard
here: a push supersedes the previous gate, and the older run keeps reporting
its own conclusion. A `cancelled` or `success` belonging to an abandoned commit
says NOTHING about the one you are about to merge.

So this tool resolves the head SHA FIRST and every verdict is scoped to it.
Runs on any other SHA are printed under SUPERSEDED and never counted.

USAGE
    python .github/ci/ci-status.py            # develop
    python .github/ci/ci-status.py --pr 969
    python .github/ci/ci-status.py --branch my-topic
    python .github/ci/ci-status.py --pr 969 --watch 60

Exit status: 0 all green for the head SHA, 1 something failed, 2 still
running/queued. That makes it usable as a gate as well as a report.

Reads the API through `gh`, so it inherits the caller's auth and stores no
token of its own.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time

REPO = "scitex-ai/scitex-agent-container"
TERMINAL_BAD = {"failure", "timed_out", "startup_failure", "action_required"}


def gh_json(path: str):
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def parse(ts: str | None):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def now():
    return dt.datetime.now(dt.timezone.utc)


def resolve_head(repo: str, pr: int | None, branch: str):
    """The SHA everything below is judged against. Resolved ONCE, up front."""
    if pr:
        d = gh_json(f"repos/{repo}/pulls/{pr}")
        if not d:
            sys.exit(f"cannot read PR #{pr} in {repo}")
        return d["head"]["sha"], d["head"]["ref"], f"PR #{pr}"
    d = gh_json(f"repos/{repo}/commits/{branch}")
    if not d:
        sys.exit(f"cannot resolve branch {branch} in {repo}")
    return d["sha"], branch, f"branch {branch}"


def collect(repo: str, ref: str):
    runs = (gh_json(f"repos/{repo}/actions/runs?branch={ref}&per_page=60") or {}).get(
        "workflow_runs", []
    )
    out = []
    for r in runs:
        jobs = (gh_json(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100") or {}).get(
            "jobs", []
        )
        out.append((r, jobs))
    return out


def job_timing(j: dict):
    created, started, done = parse(j.get("created_at")), parse(j.get("started_at")), parse(
        j.get("completed_at")
    )
    if j["status"] == "queued" or not started:
        return max(0.0, (now() - created).total_seconds()) if created else 0.0, None
    queued = max(0.0, (started - created).total_seconds()) if created else 0.0
    ran = ((done or now()) - started).total_seconds()
    return queued, ran


def report(repo: str, pr: int | None, branch: str) -> int:
    head, ref, label = resolve_head(repo, pr, branch)
    print("=" * 78)
    print(f"CI STATUS  {repo}  {label}")
    print(f"HEAD SHA   {head}   ({now().strftime('%Y-%m-%d %H:%M:%SZ')})")
    print("Every verdict below is for THIS sha. Other shas are superseded and")
    print("are NOT counted, however green they look.")
    print("=" * 78)

    current, superseded = [], {}
    for r, jobs in collect(repo, ref):
        for j in jobs:
            if r["head_sha"] == head:
                current.append((r, j))
            else:
                superseded.setdefault(r["head_sha"][:7], []).append(j["conclusion"])

    if not current:
        print("\nNO RUNS FOR THE HEAD SHA YET — the gate has not started.")

    passed, failed, running, queued_jobs = [], [], [], []
    for r, j in sorted(current, key=lambda x: x[1]["name"]):
        q, run_s = job_timing(j)
        concl, status = j.get("conclusion"), j["status"]
        if status != "completed":
            (queued_jobs if status == "queued" else running).append((j["name"], q, run_s))
        elif concl == "success":
            passed.append((j["name"], q, run_s))
        elif concl in TERMINAL_BAD:
            failed.append((j["name"], q, run_s, j.get("html_url")))
        else:
            running.append((f"{j['name']} [{concl}]", q, run_s))

    print(f"\n--- WORKING ({len(passed)} PASS) ---")
    for n, q, s in passed:
        print(f"  PASS     {n[:46]:<46} queue={q:5.0f}s run={s or 0:5.0f}s")

    print(f"\n--- BROKEN ({len(failed)} FAIL) ---")
    for n, q, s, url in failed:
        print(f"  FAIL     {n[:46]:<46} queue={q:5.0f}s run={s or 0:5.0f}s")
        print(f"           {url}")
    if not failed:
        print("  (none)")

    print(f"\n--- IN FLIGHT ({len(running)} running, {len(queued_jobs)} queued) ---")
    for n, q, s in running:
        print(f"  RUNNING  {n[:46]:<46} queue={q:5.0f}s run={s or 0:5.0f}s")
    for n, q, _ in sorted(queued_jobs, key=lambda x: -x[1]):
        print(f"  QUEUED   {n[:46]:<46} waiting={q:5.0f}s  <-- BLOCKED ON A RUNNER")

    runners = gh_json(f"repos/{repo}/actions/runners") or {}
    online = [x for x in runners.get("runners", []) if x["status"] == "online"]
    busy = [x for x in online if x["busy"]]
    local = [x for x in online if any(l["name"] == "scitex-local-cpu" for l in x["labels"])]
    print("\n--- WHY (capacity) ---")
    print(f"  runners online {len(online)}, busy {len(busy)}, "
          f"local (scitex-local-cpu) {len(local)}")
    for x in sorted(online, key=lambda x: x["name"]):
        labs = ",".join(l["name"] for l in x["labels"])
        flag = "SPARTAN-BANNED" if "spartan" in labs else ""
        print(f"    {x['name'][:34]:<34} busy={str(x['busy']):<5} {flag}")
    if queued_jobs and len(busy) >= len(local):
        print(f"  => {len(queued_jobs)} job(s) are waiting because every local slot is busy.")

    if superseded:
        print("\n--- SUPERSEDED (other shas — NOT this PR's verdict) ---")
        for sha, concls in superseded.items():
            tally = {c: concls.count(c) for c in set(concls)}
            print(f"  {sha}  {tally}")

    print("\n" + "=" * 78)
    if failed:
        print(f"VERDICT: FAIL for {head[:7]} — {len(failed)} failing job(s).")
        return 1
    if running or queued_jobs or not current:
        print(f"VERDICT: PENDING for {head[:7]} — "
              f"{len(running)} running, {len(queued_jobs)} queued. NOT green yet.")
        return 2
    print(f"VERDICT: PASS for {head[:7]} — {len(passed)} job(s) green.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--pr", type=int)
    ap.add_argument("--branch", default="develop")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="re-report every SECONDS until the head sha settles")
    a = ap.parse_args()

    while True:
        rc = report(a.repo, a.pr, a.branch)
        if not a.watch or rc != 2:
            sys.exit(rc)
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
