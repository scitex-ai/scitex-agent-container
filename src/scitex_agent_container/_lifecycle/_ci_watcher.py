"""Agent-independent CI-run watcher with DONE-gating.

A task is DONE only when its required CI is green. That claim must not
depend on any agent being alive to check it: this module polls the target
GitHub Actions run's JOB + STEP states on its own schedule, appends one
ledger row per poll (run + timestamp + per-job outcome), and refuses
completion while CI is failing, incomplete, or unverifiable.

Wire shape (``gh api`` jobs payload)::

    {"jobs": [{"name": ..., "status": "completed|in_progress|queued",
               "conclusion": "success|failure|...",
               "steps": [{"name": ..., "status": ..., "conclusion": ...}]}]}

Status vocabulary, deliberately small: ``success`` / ``failure`` /
``running`` / ``unknown``. Anything unrecognised is ``unknown`` — never
green. An empty job list is ``unknown`` (a run with no jobs observed is
not a passing run).

``health()`` answers the liveness question separately from the verdict:
``healthy`` requires a FRESH success. A missing or stale ledger reads
``stalled`` — a stopped runner must never read as healthy.

All collaborators (``fetch``, ``now_fn``) are injection seams; the only
shared state is the JSONL ledger file, so a cron/systemd unit, a
``sac listen`` side task, or a human can poll and gate with no agent in
the loop.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_RUNNING = "running"
STATUS_UNKNOWN = "unknown"

HEALTHY = "healthy"
FAILING = "failing"
HEALTH_RUNNING = "running"
STALLED = "stalled"

#: A ledger older than this is unverifiable, not green.
DEFAULT_STALE_AFTER_S = 600.0

#: ``gh api`` conclusions that mean the job failed. ``cancelled`` counts:
#: a cancelled run is unfinished work whose verdict nobody has.
_FAILURE_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
        "stale",
        "cancelled",
    }
)

#: Job/step ``status`` values that mean "still going".
_ACTIVE_STATUSES = frozenset({"queued", "in_progress", "pending", "waiting"})


class DoneRefused(Exception):
    """Raised by :func:`assert_done_allowed` while CI is failing/incomplete/unverifiable."""


def _utcnow_iso(now_fn: Callable[[], float] | None = None) -> tuple[str, float]:
    """Return ``(iso_timestamp, epoch)`` for now (``now_fn`` seam, epoch seconds)."""
    epoch = now_fn() if now_fn is not None else time.time()
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    return iso, epoch


def _step_status(step: Any) -> str:
    """Reduce one jobs-API step to the four-state vocabulary."""
    if not isinstance(step, dict):
        return STATUS_UNKNOWN
    conclusion = str(step.get("conclusion") or "").strip().lower()
    if conclusion:
        if conclusion == "success":
            return STATUS_SUCCESS
        if conclusion in _FAILURE_CONCLUSIONS:
            return STATUS_FAILURE
        if conclusion == "skipped":
            return STATUS_SUCCESS
        return STATUS_UNKNOWN
    status = str(step.get("status") or "").strip().lower()
    if status == "completed":
        return STATUS_UNKNOWN  # completed without a conclusion: unverifiable
    if status in _ACTIVE_STATUSES or status:
        return STATUS_RUNNING if status in _ACTIVE_STATUSES else STATUS_UNKNOWN
    return STATUS_UNKNOWN


def _job_status(job: Any) -> tuple[str, dict]:
    """Reduce one jobs-API job to ``(status, {step_name: status})``."""
    if not isinstance(job, dict):
        return STATUS_UNKNOWN, {}
    steps: dict = {}
    for step in job.get("steps") or []:
        if isinstance(step, dict) and step.get("name"):
            steps[str(step["name"])] = _step_status(step)
    conclusion = str(job.get("conclusion") or "").strip().lower()
    if conclusion:
        if conclusion == "success":
            rolled = STATUS_SUCCESS
        elif conclusion in _FAILURE_CONCLUSIONS:
            rolled = STATUS_FAILURE
        else:
            rolled = STATUS_UNKNOWN
    else:
        status = str(job.get("status") or "").strip().lower()
        if status == "completed":
            rolled = STATUS_UNKNOWN
        elif status in _ACTIVE_STATUSES:
            rolled = STATUS_RUNNING
        elif status:
            rolled = STATUS_UNKNOWN
        else:
            rolled = STATUS_UNKNOWN
    # A job that claims success while a step failed is not success: the
    # step states are the finer sensor and a contradiction between the two
    # must fail closed, never open.
    if rolled == STATUS_SUCCESS and STATUS_FAILURE in steps.values():
        rolled = STATUS_FAILURE
    return rolled, steps


def roll_up_status(jobs: Any) -> tuple[str, dict, dict]:
    """Roll a jobs-API job list up to ``(run_status, {job: status}, {job: {step: status}})``.

    Precedence: any failure → failure; else any running → running; else
    all-success (non-empty) → success; empty / unparseable → unknown.
    """
    per_job: dict = {}
    per_step: dict = {}
    if not isinstance(jobs, list):
        return STATUS_UNKNOWN, per_job, per_step
    for job in jobs:
        if isinstance(job, dict) and job.get("name") is not None:
            name = str(job["name"])
        else:
            name = "<unnamed>"
        status, steps = _job_status(job)
        per_job[name] = status
        per_step[name] = steps
    if not per_job:
        return STATUS_UNKNOWN, per_job, per_step
    values = set(per_job.values())
    if STATUS_FAILURE in values:
        return STATUS_FAILURE, per_job, per_step
    if STATUS_RUNNING in values:
        return STATUS_RUNNING, per_job, per_step
    if values == {STATUS_SUCCESS}:
        return STATUS_SUCCESS, per_job, per_step
    return STATUS_UNKNOWN, per_job, per_step


def _default_fetch(args: list) -> str:
    """Run ``gh <args>`` and return stdout (empty on any failure → unknown, never green)."""
    import subprocess

    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False
        )
    except Exception:
        return ""
    return proc.stdout or ""


def fetch_run_jobs(
    repo: str,
    run_id: int | str,
    *,
    fetch: Callable[[list], str] = _default_fetch,
) -> tuple[str, dict, dict]:
    """Poll one workflow run's job/step states; return :func:`roll_up_status` triple.

    A fetch error / unparseable payload yields ``unknown`` — the watcher
    records that it could not verify, it does not invent a verdict.
    """
    raw = fetch(
        ["api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"]
    )
    try:
        payload = json.loads(raw) if (raw or "").strip() else {}
    except (ValueError, TypeError):
        payload = {}
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return roll_up_status(jobs)


def record_snapshot(
    ledger: str | Path,
    *,
    repo: str,
    run_id: int | str,
    status: str,
    per_job: dict | None = None,
    per_step: dict | None = None,
    now_fn: Callable[[], float] | None = None,
) -> dict:
    """Append one poll row to the JSONL ledger; return the row."""
    recorded_at, _ = _utcnow_iso(now_fn)
    row = {
        "repo": repo,
        "run_id": str(run_id),
        "recorded_at": recorded_at,
        "status": status,
        "jobs": dict(per_job or {}),
        "steps": {k: dict(v) for k, v in (per_step or {}).items()},
    }
    path = Path(ledger)
    if path.parent != Path(".") and str(path.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def read_latest(
    ledger: str | Path, *, repo: str, run_id: int | str
) -> dict | None:
    """Return the newest ledger row for ``(repo, run_id)``, or ``None``."""
    path = Path(ledger)
    if not path.exists():
        return None
    want = str(run_id)
    latest: dict | None = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(row, dict)
                and row.get("repo") == repo
                and str(row.get("run_id")) == want
            ):
                latest = row
    return latest


def _row_age_s(row: dict, now_epoch: float) -> float:
    """Seconds since ``row`` was recorded; unparseable timestamp → infinite (stale)."""
    try:
        ts = datetime.fromisoformat(str(row.get("recorded_at", "")))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return now_epoch - ts.timestamp()
    except (ValueError, TypeError):
        return float("inf")


def health(
    ledger: str | Path,
    *,
    repo: str,
    run_id: int | str,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    now_fn: Callable[[], float] | None = None,
) -> str:
    """Liveness verdict for a watched run: healthy / failing / running / stalled.

    Only a FRESH success is ``healthy``. Missing ledger, unparseable or
    stale record → ``stalled``: a stopped watcher/runner must never read
    as healthy, and ``stalled`` is distinct from ``failing`` so the
    operator can tell "CI is red" from "nobody has looked lately".
    """
    row = read_latest(ledger, repo=repo, run_id=run_id)
    now_epoch = now_fn() if now_fn is not None else time.time()
    if row is None:
        return STALLED
    if _row_age_s(row, now_epoch) > stale_after_s:
        return STALLED
    status = str(row.get("status", STATUS_UNKNOWN))
    if status == STATUS_SUCCESS:
        return HEALTHY
    if status == STATUS_FAILURE:
        return FAILING
    if status == STATUS_RUNNING:
        return HEALTH_RUNNING
    return STALLED


def assert_done_allowed(
    ledger: str | Path,
    *,
    repo: str,
    run_id: int | str,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    now_fn: Callable[[], float] | None = None,
) -> dict:
    """Gate a task's DONE transition on required-CI success; return the green row.

    Raises :class:`DoneRefused` while CI is failing, incomplete
    (running/unknown), or unverifiable (no record, stale record,
    unparseable timestamp). No record shape reads as permission.
    """
    row = read_latest(ledger, repo=repo, run_id=run_id)
    if row is None:
        raise DoneRefused(
            f"DONE refused for {repo} run {run_id}: no watcher record — "
            "CI state is unverifiable (watcher never polled this run)."
        )
    now_epoch = now_fn() if now_fn is not None else time.time()
    if _row_age_s(row, now_epoch) > stale_after_s:
        raise DoneRefused(
            f"DONE refused for {repo} run {run_id}: latest record "
            f"({row.get('recorded_at')}) is older than {stale_after_s}s — "
            "CI state is unverifiable (watcher may be stopped)."
        )
    status = str(row.get("status", STATUS_UNKNOWN))
    if status != STATUS_SUCCESS:
        raise DoneRefused(
            f"DONE refused for {repo} run {run_id}: CI is {status}, "
            "not success — required CI must be green."
        )
    return row


def poll_once(
    ledger: str | Path,
    *,
    repo: str,
    run_id: int | str,
    fetch: Callable[[list], str] = _default_fetch,
    now_fn: Callable[[], float] | None = None,
) -> dict:
    """Poll one run's job/step states and record the outcome; return the row.

    One poll = one ledger row, always — including ``unknown`` when GitHub
    could not be read. A failed fetch is recorded as unverifiable, never
    mistaken for green, and never silently dropped.
    """
    status, per_job, per_step = fetch_run_jobs(repo, run_id, fetch=fetch)
    return record_snapshot(
        ledger,
        repo=repo,
        run_id=run_id,
        status=status,
        per_job=per_job,
        per_step=per_step,
        now_fn=now_fn,
    )


def watch_forever(
    ledger: str | Path,
    *,
    targets: list[tuple[str, Any]],
    interval_s: float = 60.0,
    fetch: Callable[[list], str] = _default_fetch,
    now_fn: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    stop_after: int | None = None,
) -> int:
    """Poll every ``(repo, run_id)`` target each tick; return polls recorded.

    The agent-independent entry point: run this under cron/systemd (or
    ``sac listen`` lifespan) — no agent turn is involved. ``stop_after``
    bounds ticks for tests; ``None`` runs until killed. A per-target
    exception is recorded as ``unknown`` and the loop continues: one
    unreadable run must not blind the rest.
    """
    import time as _time

    sleep = sleep_fn if sleep_fn is not None else _time.sleep
    polls = 0
    ticks = 0
    while True:
        for repo, run_id in targets:
            try:
                poll_once(
                    ledger, repo=repo, run_id=run_id, fetch=fetch, now_fn=now_fn
                )
            except Exception:
                record_snapshot(
                    ledger,
                    repo=repo,
                    run_id=run_id,
                    status=STATUS_UNKNOWN,
                    now_fn=now_fn,
                )
            polls += 1
        ticks += 1
        if stop_after is not None and ticks >= stop_after:
            return polls
        sleep(interval_s)


# --- host load -----------------------------------------------------------
#
# Companion leg to the CI-run poll above: while CI is queued, are the
# self-hosted compute hosts actually working? Each poll ssh-probes
# scitex-compute-02/03/04 for 1-min load plus Runner/apptainer/pytest
# process counts and records one row in its OWN ledger file. The file is
# separate on purpose: CI rows gate DONE via ``read_latest()``, and a
# host-load row must never be mistaken for a CI verdict.
#
# Observed idle 2026-10-08: 1-min loads 0.6-2.0 with no test procs while
# CI had queued work — the ``idle_while_queued`` flag names exactly that.

#: Compute hosts sampled by the host-load leg.
HOST_LOAD_HOSTS = ("scitex-compute-02", "scitex-compute-03", "scitex-compute-04")

#: A host with 1-min load below this AND no pytest procs reads idle.
IDLE_LOAD1_BELOW = 2.0

#: One ssh per host prints four lines: /proc/loadavg, then Runner.,
#: apptainer, and pytest process counts. Bracket-trick patterns (``[R]``)
#: so the probing grep never counts itself.
_HOST_PROBE_CMD = (
    "cat /proc/loadavg; "
    "ps -eo args= | grep -c '[R]unner\\.'; "
    "ps -eo args= | grep -c '[a]pptainer'; "
    "ps -eo args= | grep -c '[p]ytest'"
)


def _default_ssh(host: str, remote_cmd: str) -> str:
    """Run ``ssh <host> <remote_cmd>`` and return stdout (empty on failure)."""
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                host,
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout or ""


def parse_host_probe(output: str) -> dict:
    """Parse one host's probe output into a snapshot dict.

    Never raises: garbage in yields an ``{"error": ...}`` entry, never an
    idle verdict — idle-while-queued must not fire on data nobody has.
    """
    try:
        lines = (output or "").strip().splitlines()
        load1, load5, load15 = (float(x) for x in lines[0].split()[:3])
        runner = int(lines[1].strip())
        apptainer = int(lines[2].strip())
        pytest_n = int(lines[3].strip())
    except (ValueError, IndexError):
        return {"error": "unparseable probe output"}
    snap = {
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "runner": runner,
        "apptainer": apptainer,
        "pytest": pytest_n,
    }
    snap["idle"] = bool(load1 < IDLE_LOAD1_BELOW and pytest_n == 0)
    return snap


def fetch_host_loads(
    hosts: Any = HOST_LOAD_HOSTS,
    *,
    ssh: Callable[[str, str], str] = _default_ssh,
) -> dict:
    """Probe every host; return ``{host: snapshot}``.

    An ssh failure records an ``{"error": ...}`` entry (never idle): one
    unreachable host must not read as an idle fleet.
    """
    snapshots: dict = {}
    for host in hosts or ():
        try:
            snapshots[str(host)] = parse_host_probe(ssh(str(host), _HOST_PROBE_CMD))
        except Exception:
            snapshots[str(host)] = {"error": "ssh failed"}
    return snapshots


def idle_while_queued(hosts_snapshot: Any, queued: bool) -> bool:
    """True only when runs are queued/in-progress AND every probed host is idle.

    Fail-closed: no queued work, empty input, any error entry, or any
    non-idle host → False.
    """
    if not queued or not isinstance(hosts_snapshot, dict) or not hosts_snapshot:
        return False
    return all(
        isinstance(snap, dict) and snap.get("idle") is True
        for snap in hosts_snapshot.values()
    )


def record_host_loads(
    ledger: str | Path,
    *,
    queued: bool,
    hosts: dict | None = None,
    now_fn: Callable[[], float] | None = None,
) -> dict:
    """Append one host-load row to its own JSONL ledger; return the row."""
    recorded_at, _ = _utcnow_iso(now_fn)
    hosts = dict(hosts or {})
    row = {
        "type": "host-load",
        "recorded_at": recorded_at,
        "queued": bool(queued),
        "hosts": {h: dict(s) for h, s in hosts.items()},
        "idle_while_queued": idle_while_queued(hosts, queued),
    }
    path = Path(ledger)
    if path.parent != Path(".") and str(path.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def read_latest_host_load(ledger: str | Path) -> dict | None:
    """Return the newest host-load row, or ``None`` when there is none."""
    path = Path(ledger)
    if not path.exists():
        return None
    latest: dict | None = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("type") == "host-load":
                latest = row
    return latest


def poll_host_loads_once(
    ledger: str | Path,
    *,
    queued: bool = False,
    hosts: Any = HOST_LOAD_HOSTS,
    ssh: Callable[[str, str], str] = _default_ssh,
    now_fn: Callable[[], float] | None = None,
) -> dict:
    """Probe all hosts once and record the outcome; return the row.

    One poll = one row, always — including error entries when a host
    cannot be read. ``queued`` (runs queued/in-progress per the Actions
    API) is supplied by the caller; the flag logic stays a pure function
    of that plus the snapshots.
    """
    return record_host_loads(
        ledger, queued=queued, hosts=fetch_host_loads(hosts, ssh=ssh), now_fn=now_fn
    )
