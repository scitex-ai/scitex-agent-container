"""Deploy-freshness reconciler: FAIL LOUD when the checkout is behind origin/develop.

INCIDENT 2026-07-02: a full day of merged PRs silently never deployed. The
host checkout sat 18 commits behind ``origin/develop`` (cleanly
fast-forwardable), the auto-pull cron was not running, and NOTHING
surfaced the staleness — so agents ran stale code without knowing.
Operator directive: make deploy-staleness a FAIL-LOUD signal, not silent
drift.

This is the deterministic reconciler that runs INSIDE ``sac listen``,
periodically compares the running checkout against ``origin/develop``, and
ALARMS on a mismatch so stale code can never sit silent again. It mirrors
the shape of :mod:`_liveness_tick` (the anchored-stop reconciler): pure
detection rule behind injected seams + production seams + an async loop
launched by the lifespan.

SEPARATION OF CONCERNS (same as the liveness-tick producer): **sac only
DETECTS and EMITS.** sac does NOT pull, reset, or otherwise mutate the
checkout — a deploy-freshness alarm is a report, not an action. We (1) log
a LOUD warning naming how-many-behind + the newest undeployed commit
subjects, and (2) emit an anomaly event on the SAME ``scitex_todo.hooks``
entry-point bus the liveness-tick reconciler uses. scitex-todo's own
consumer (registered separately, on their side) turns it into a card
record + operator push. Nothing here writes ``tasks.yaml`` or touches git
state.

Bind-safety (cards ``sac-listen-self-peer-persist-blocks-bind`` /
``sac-listen-watchdog-autorestart-alarm``): the only blocking IO — a
``git fetch`` + two ``git`` reads — runs through
:func:`_off_loop.run_blocking_or`, so a slow/wedged ``git fetch`` can
NEVER starve uvicorn's bind or the running server.

SCOPE (this PR = the **checkout-vs-origin** axis only): we detect one
kind of staleness — the checkout is behind ``origin/develop``. Two other
deploy-drift axes are follow-ons and deliberately NOT built here:

  * NOTE(follow-on): **daemon-vs-checkout** — the running ``sac listen``
    process was launched from an OLDER checkout than the current HEAD
    (a merge landed but the daemon was never restarted).
  * NOTE(follow-on): **SIF-vs-develop** — the built container image
    (``.sif``) lags the source at ``origin/develop`` (a rebuild is due).

Event shape::

    {"kind": "deploy-stale", "commits_behind": int,
     "newest_subjects": [str, ...], "severity": "warning"|"critical"}

``severity`` is ``"critical"`` once the checkout is >= ``CRITICAL_BEHIND``
commits behind, else ``"warning"``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# --- env knobs (git fetch is heavier than a registry read → slow cadence) ---
ENV_DISABLED = "SAC_DEPLOY_FRESHNESS_DISABLED"
ENV_INTERVAL_S = "SAC_DEPLOY_FRESHNESS_INTERVAL_S"
ENV_CHECKOUT = "SAC_DEPLOY_CHECKOUT"

DEFAULT_INTERVAL_S = 600.0  # 10 min — a git fetch is heavier than an FS read.

# Escalate to "critical" once the checkout is this many commits behind.
CRITICAL_BEHIND = 10

# Cap how many newest-undeployed subjects we carry in the alarm/log.
MAX_SUBJECTS = 5

# The entry-point bus sac emits an anomaly onto — the SAME group the
# liveness-tick reconciler uses. scitex-todo registers its consumer here
# (separately, on their side); until then the emit degrades to the loud log.
HOOKS_ENTRY_POINT_GROUP = "scitex_todo.hooks"


# ---------------------------------------------------------------------------
# pure detection rule (trivially testable — no IO, no seams)
# ---------------------------------------------------------------------------


def build_staleness_alarm(
    commits_behind: int, newest_subjects: list[str]
) -> dict | None:
    """Return an alarm payload when the checkout is behind, else ``None``.

    Pure. ``commits_behind <= 0`` (fresh, or an unknown/degraded git read
    that resolved to ``0``) ⇒ ``None`` — no alarm. Otherwise a dict::

        {"kind": "deploy-stale", "commits_behind": int,
         "newest_subjects": [...], "severity": "warning"|"critical"}

    ``severity`` is ``"critical"`` once ``commits_behind >=
    CRITICAL_BEHIND`` (the 18-behind incident would have been critical),
    else ``"warning"``.
    """
    if commits_behind <= 0:
        return None
    severity = "critical" if commits_behind >= CRITICAL_BEHIND else "warning"
    return {
        "kind": "deploy-stale",
        "commits_behind": int(commits_behind),
        "newest_subjects": list(newest_subjects),
        "severity": severity,
    }


# ---------------------------------------------------------------------------
# reconcile — detection rule + loud log + emit (all collaborators injected)
# ---------------------------------------------------------------------------


def reconcile_deploy_freshness(
    *,
    count_behind: Callable[[], tuple[int, list[str]]],
    emit: Callable[[dict], Any],
    log: logging.Logger | None = None,
) -> dict | None:
    """One reconcile pass: detect staleness, FAIL LOUD, emit — return the alarm.

    All collaborators are INJECTED seams so tests need no mocks:

      * ``count_behind()`` → ``(commits_behind, newest_subjects)``. In
        production this is :func:`production_count_behind` bound to the
        resolved checkout; a broken git read returns ``(0, [])`` so a
        degraded read can NEVER raise a false alarm.
      * ``emit(alarm)`` → deliver the alarm onto the ``scitex_todo.hooks``
        bus. In production this is :func:`production_emit`.
      * ``log`` → the logger to scream on (defaults to the module logger).

    When :func:`build_staleness_alarm` yields a payload we (1) log a LOUD
    warning naming how-many-behind + the newest undeployed commit subjects
    and (2) call ``emit(alarm)``. Returns the alarm dict (or ``None`` when
    fresh) so callers/tests can assert on it.
    """
    _log = log if log is not None else logger
    commits_behind, newest_subjects = count_behind()
    alarm = build_staleness_alarm(commits_behind, newest_subjects)
    if alarm is None:
        return None
    _log.warning(
        "deploy_freshness: STALE — checkout is %d commit(s) behind "
        "origin/develop (severity=%s). Merged-but-undeployed code is "
        "running. Newest undeployed: %s. FAST-FORWARD THE CHECKOUT "
        "(and check the auto-pull cron).",
        alarm["commits_behind"],
        alarm["severity"],
        "; ".join(newest_subjects) if newest_subjects else "(subjects unknown)",
    )
    emit(alarm)
    return alarm


# ---------------------------------------------------------------------------
# production seams — checkout resolution + the blocking git reads
# ---------------------------------------------------------------------------


def resolve_checkout() -> Path | None:
    """Resolve the git checkout to compare against ``origin/develop``.

    Precedence:

      1. ``$SAC_DEPLOY_CHECKOUT`` env override (an explicit checkout path);
      2. else derive from the INSTALLED package location — walk up from
         ``scitex_agent_container.__file__`` looking for a ``.git`` entry
         (dir OR file, so a worktree's ``.git`` gitlink also counts);
      3. else ``None`` (skip — a pip-installed sdist has no ``.git``, so
         there is nothing to compare and we must not alarm).

    Fail-soft: any error resolving the package path ⇒ ``None``.
    """
    override = os.environ.get(ENV_CHECKOUT, "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    try:
        import scitex_agent_container

        start = Path(scitex_agent_container.__file__).resolve()
    except Exception:  # stx-allow: fallback (no importable pkg path → nothing to compare)
        return None
    for parent in start.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _git(checkout: Path, *args: str, timeout: float = 30.0) -> str:
    """Run ``git -C <checkout> <args>`` and return stdout (stripped).

    Raises :class:`subprocess.CalledProcessError` on a non-zero exit so the
    caller can fail-soft. ``check=True`` + captured output.
    """
    proc = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return proc.stdout.strip()


def production_count_behind(checkout: Path) -> tuple[int, list[str]]:
    """How many commits ``checkout`` is behind ``origin/develop`` + subjects.

    Runs ``git -C <checkout> fetch origin develop`` then
    ``git rev-list --count HEAD..origin/develop`` and
    ``git log --oneline HEAD..origin/develop`` (newest first, capped at
    :data:`MAX_SUBJECTS`).

    Fail-soft: ANY git error (fetch failure, offline, detached repo,
    missing remote) ⇒ ``(0, [])``. A broken git read must NEVER raise a
    false alarm — a real-but-unverifiable staleness is safer left silent
    than a fabricated one screamed loud.
    """
    try:
        _git(checkout, "fetch", "origin", "develop")
        raw_count = _git(checkout, "rev-list", "--count", "HEAD..origin/develop")
        count = int(raw_count.strip() or "0")
    except (subprocess.SubprocessError, OSError, ValueError):  # stx-allow: fallback (unreadable git → no false alarm)
        return 0, []
    if count <= 0:
        return 0, []
    try:
        raw_log = _git(
            checkout,
            "log",
            "--oneline",
            f"-{MAX_SUBJECTS}",
            "HEAD..origin/develop",
        )
    except (subprocess.SubprocessError, OSError):  # stx-allow: fallback (count is trustworthy even if subjects are not)
        raw_log = ""
    subjects = [line for line in raw_log.splitlines() if line.strip()]
    return count, subjects


def production_count_behind_or_zero() -> tuple[int, list[str]]:
    """Resolve the checkout then :func:`production_count_behind`, or ``(0, [])``.

    Used as the production ``count_behind`` seam: when no checkout can be
    resolved (pip-installed, no ``.git``) there is nothing to compare, so
    we return ``(0, [])`` and no alarm fires.
    """
    checkout = resolve_checkout()
    if checkout is None:
        return 0, []
    return production_count_behind(checkout)


# ---------------------------------------------------------------------------
# production emit — reuse the SAME scitex_todo.hooks bus as liveness-tick
# ---------------------------------------------------------------------------


def production_emit(alarm: dict) -> int:
    """Emit ``alarm`` onto the ``scitex_todo.hooks`` bus. Returns delivered count.

    Reuses the liveness-tick producer's graceful bus plumbing verbatim
    (``_load_hook_consumers`` + ``emit_anomaly``) rather than re-deriving
    the entry-point lookup, so both reconcilers stay on ONE emit path.
    Degrades gracefully: no registered consumer ⇒ 0 delivered, and the
    loud log in :func:`reconcile_deploy_freshness` still fires.
    """
    from ._liveness_tick import _load_hook_consumers, emit_anomaly

    return emit_anomaly(alarm, _load_hook_consumers())


def _production_reconcile_once() -> dict | None:
    """The production one-tick pass: real seams wired in. Blocking (git IO)."""
    return reconcile_deploy_freshness(
        count_behind=production_count_behind_or_zero,
        emit=production_emit,
    )


# ---------------------------------------------------------------------------
# async loop — launched by the sac listen lifespan
# ---------------------------------------------------------------------------


async def deploy_freshness_loop(
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    reconcile_once: Callable[[], dict | None] | None = None,
) -> None:
    """Long-running task launched by the ``sac listen`` lifespan.

    Each tick runs one deploy-freshness reconcile pass and, when the
    checkout is behind ``origin/develop``, FAILS LOUD (a warning log +
    an emit on ``scitex_todo.hooks``).

    Two lessons baked in (both cost the fleet an incident already):

      * **SLEEP BEFORE THE FIRST TICK.** ``asyncio.sleep(interval_s)`` is
        at the TOP of the ``while`` — a startup tick would pollute short
        test lifespans and buys nothing (a fresh boot is, by definition,
        freshly deployed).
      * **Dispatch the blocking pass OFF the event loop** via
        :func:`_off_loop.run_blocking_or`, so a slow ``git fetch`` can
        never starve uvicorn's bind or the running server. A pass that
        times out / errors degrades to ``None`` (no alarm this tick),
        fail-loud-logged by ``run_blocking_or`` itself.

    Injection seam (tests): ``reconcile_once`` replaces the production
    pass; production leaves it ``None``. Cancellation is honoured at the
    sleep boundary and re-raised cleanly.
    """
    pass_fn = reconcile_once if reconcile_once is not None else _production_reconcile_once
    logger.info("deploy_freshness: starting (interval_s=%.1f)", interval_s)
    try:
        while True:
            # SLEEP BEFORE THE FIRST TICK — no startup tick (see docstring).
            await asyncio.sleep(interval_s)
            try:
                # The pass does a blocking ``git fetch`` + reads; run it OFF
                # the event loop with a hard timeout so a wedged fetch can
                # never starve the bind. A timeout/error degrades to None.
                from .._lifecycle._off_loop import run_blocking_or

                await run_blocking_or(
                    pass_fn,
                    default=None,
                    op="deploy_freshness reconcile (git fetch + rev-list)",
                    timeout_s=max(interval_s, 45.0),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # stx-allow: fallback (loop must not die on a transient git/IO error)
                logger.warning(
                    "deploy_freshness: tick failed (%s); sleeping + retry", exc
                )
    except asyncio.CancelledError:
        logger.info("deploy_freshness: cancelled cleanly")
        raise


__all__ = [
    "CRITICAL_BEHIND",
    "DEFAULT_INTERVAL_S",
    "ENV_CHECKOUT",
    "ENV_DISABLED",
    "ENV_INTERVAL_S",
    "HOOKS_ENTRY_POINT_GROUP",
    "MAX_SUBJECTS",
    "build_staleness_alarm",
    "deploy_freshness_loop",
    "production_count_behind",
    "production_count_behind_or_zero",
    "production_emit",
    "reconcile_deploy_freshness",
    "resolve_checkout",
]
