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


def _required_runner_labels(repo: str) -> set[str]:
    """The labels a job must match, read from the repo's own CI_RUNS_ON.

    Returns an empty set when the variable is unset or unparseable, and the
    caller then reports every online runner rather than pretending to know
    which pool is targeted. Guessing a default here is what produced the bug
    this replaces: a hardcoded label that no longer matched reality made the
    capacity report confidently wrong instead of visibly unknown.
    """
    raw = gh_json(f"repos/{repo}/actions/variables/CI_RUNS_ON") or {}
    try:
        return {str(x) for x in json.loads(raw.get("value", ""))}
    except (ValueError, TypeError):
        return set()


#: Exit code for "the instrument could not read", kept distinct from the
#: verdict codes (0 green / 1 failed / 2 running) on purpose: a report nobody
#: could produce must never be scored as one of the answers. Anything using
#: this script as a gate can then treat 3 as "ask again", not as a pass and
#: not as a failure.
EXIT_UNMEASURED = 3


def _stop_unmeasured(what: str, path: str, hint: str) -> None:
    """Print why no verdict exists and exit ``EXIT_UNMEASURED``.

    Separate from ``sys.exit(msg)`` deliberately: that form always exits 1,
    which is this tool's code for "CI FAILED". Reporting an unreadable API as
    a red build is the same error class the script exists to prevent — a
    verdict asserted about something nobody looked at.
    """
    print(f"UNMEASURED: {what}", file=sys.stderr)
    print(f"  failing path: {path}", file=sys.stderr)
    print(f"  {hint}", file=sys.stderr)
    sys.exit(EXIT_UNMEASURED)


def gh_json(path: str):
    """One `gh api` call, or a LOUD stop when the API refused to answer.

    A REFUSAL IS NOT A DATA POINT. This used to fold every failure into
    ``None`` — a 404, a 403 rate limit and a 401 all became the same value —
    and callers then reported the *default* as the finding. Measured
    2026-08-20: with the secondary rate limit active, this tool printed
    "cannot resolve branch develop", which reads as "that branch is gone".
    The branch was fine; GitHub was refusing.

    Rate limit and auth failures are not partial data either: every
    subsequent call fails the same way, so continuing yields a report made
    entirely of false negatives — green-looking because nothing could be
    read. So those two stop the run with ``EXIT_UNMEASURED``. A genuine 404
    still returns ``None``, because "this object does not exist" IS an
    answer and callers legitimately branch on it.

    Note that ``gh api rate_limit`` will happily report thousands remaining
    while this is happening: the SECONDARY limit does not appear there. The
    only reliable signal is the refusal itself, which is why it is read here
    rather than pre-checked.
    """
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    blob = (p.stdout or "") + (p.stderr or "")
    low = blob.lower()
    if "rate limit" in low or "secondary rate" in low:
        _stop_unmeasured(
            "GitHub is rate-limiting this token, so no verdict can be produced.",
            path,
            "`gh api rate_limit` may still show quota — the SECONDARY limit is "
            "invisible there. Retry in a few minutes.",
        )
    if "bad credentials" in low or "requires authentication" in low:
        _stop_unmeasured(
            "GitHub rejected the credentials, so no verdict can be produced.",
            path,
            "Run `gh auth status`.",
        )
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

    # CAPACITY IS ABOUT THE POOL THE JOBS ACTUALLY TARGET.
    #
    # This block used to read only `repos/{repo}/actions/runners` and count the
    # ones labelled `scitex-local-cpu`. Both halves were wrong, and together
    # they made the tool answer confidently about a pool nothing runs on:
    #
    #   * `vars.CI_RUNS_ON` for this repo is ["self-hosted","Linux","X64",
    #     "scitex-org-cpu"], so every job lands on ORG runners. The repo-level
    #     endpoint cannot see them.
    #   * No runner in either pool carries `scitex-local-cpu` except the single
    #     repo runner, so `local` was ~1 and `len(busy) >= len(local)` fired
    #     on almost any input — a "diagnosis" that is nearly a constant.
    #
    # Measured 2026-08-20, when 22 PRs were queued: the old block would have
    # reported the repo pool as 1 online / 0 busy and implied there was no
    # contention at all. The truth was all four `scitex-org-cpu` runners busy
    # and four Spartan runners idle and INELIGIBLE. Read the variable, resolve
    # the pool it names, and report eligibility — never a fixed label.
    org = repo.split("/", 1)[0]
    want = _required_runner_labels(repo)
    seen: dict[str, dict] = {}
    for endpoint in (f"repos/{repo}/actions/runners", f"orgs/{org}/actions/runners"):
        for x in (gh_json(endpoint) or {}).get("runners", []):
            seen.setdefault(x["name"], x)
    online = [x for x in seen.values() if x["status"] == "online"]

    def _labels(x):
        return {l["name"] for l in x["labels"]}

    eligible = [x for x in online if want <= _labels(x)] if want else online
    busy = [x for x in eligible if x["busy"]]
    idle_ineligible = [x for x in online if not x["busy"] and x not in eligible]

    print("\n--- WHY (capacity) ---")
    print(f"  CI_RUNS_ON requires {sorted(want) if want else '(unset — showing all)'}")
    print(f"  online {len(online)}, eligible {len(eligible)}, "
          f"busy {len(busy)}, idle-but-ineligible {len(idle_ineligible)}")
    for x in sorted(online, key=lambda x: x["name"]):
        mark = "eligible" if x in eligible else "NOT-ELIGIBLE"
        print(f"    {x['name'][:34]:<34} busy={str(x['busy']):<5} {mark:<12} "
              f"[{','.join(sorted(_labels(x)))}]")
    if queued_jobs and eligible and len(busy) >= len(eligible):
        print(f"  => {len(queued_jobs)} job(s) are waiting: every ELIGIBLE runner is busy.")
        if idle_ineligible:
            # The actionable half. A saturated pool next to idle machines is a
            # LABEL problem, not a hardware one, and naming it is the whole
            # point of printing eligibility rather than a raw busy count.
            print(f"  => {len(idle_ineligible)} runner(s) are idle but do not carry "
                  f"{sorted(want)} — widen the labels or the variable, not the fleet.")
    elif queued_jobs and not eligible:
        print(f"  => {len(queued_jobs)} job(s) can NEVER dispatch: no online runner "
              f"carries {sorted(want)}.")

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
