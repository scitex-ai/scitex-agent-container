"""Launch-time LOCAL spec-source drift check.

When ``sac agents start`` (or any lifecycle path that loads a spec)
runs, the agent's spec.yaml may have come from a git-tracked source
directory — on these hosts ``~/.scitex/agent-container/agents`` is a
symlink into ``~/.dotfiles/src/.scitex/...``. If that source repo is
stale (behind its remote) or has unpushed local commits (ahead /
diverged), the agent might run an old spec or have a spec that never
propagates to other hosts.

This module compares the spec source's git working tree against its
upstream tracking branch and returns a :class:`DriftStatus`.

Design constraints (per the work item):

* **FAST** — a single ``git fetch`` per repo, cached for
  ``_FETCH_TTL_S`` seconds so repeated launches don't pay the network
  cost. The rev-list compare itself is local + instant.
* **RESILIENT** — never raises. A missing git binary, a
  non-repo source dir, an unreachable remote, or any subprocess error
  degrades to ``NOT_A_REPO`` / ``UNREACHABLE`` and the launch proceeds.
* **DEFAULT = REFUSE on a STALE spec** (operator ruling 2026-08-10:
  「スペックがおかしかったら起動不可っていうのをデフォルトに」). A spec source
  that is BEHIND / DIVERGED may launch an old spec, so the start is
  refused and the named override ``--allow-stale-spec`` /
  ``SAC_ALLOW_STALE_SPEC=1`` is the way past it.

  AHEAD is deliberately NOT a refusal — see :attr:`DriftStatus.is_stale`.
  Unpushed local commits mean the spec will not PROPAGATE; the spec about
  to launch is still the newest one that exists, and hosts like spartan
  legitimately carry local commits. AHEAD stays a loud warning.

  NOT_A_REPO / UNREACHABLE never refuse: drift is *unknown* there, not
  present, and refusing on "I could not check" would ground every agent
  the moment the network hiccups.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from .._logging import get_logger
from ._status import DriftState, DriftStatus

# How long a successful ``git fetch`` for a given repo stays "fresh".
# Repeated ``sac agents start`` within this window skip the network
# round-trip and reuse the cached fetch. Keeps the hot path cheap.
_FETCH_TTL_S = 60

# Per-git-subprocess wall-clock cap. ``git fetch`` against an
# unreachable remote must not hang a launch; we bound it and treat a
# timeout as UNREACHABLE.
_GIT_TIMEOUT_S = 15

# The NAMED override for the stale-spec refusal. One flag per condition —
# never a blanket ``--force`` / ``--ignore-warnings``, which skips every check
# at once and leaves no record of WHICH one was bypassed.
ALLOW_STALE_FLAG = "--allow-stale-spec"
ALLOW_STALE_ENV = "SAC_ALLOW_STALE_SPEC"


def _run_git(repo: Path, *args: str, timeout: int = _GIT_TIMEOUT_S):
    """Run ``git -C <repo> <args>``; return the CompletedProcess.

    Real subprocess (no mocks). Tests install a ``git`` shim on PATH or
    operate on a real ``git init`` repo. Never raises — a missing git
    binary or a timeout is surfaced via the return value.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def spec_source_repo(spec_path: str | Path) -> Path | None:
    """Return the git working-tree root that contains ``spec_path``.

    Resolves the path first (so a symlinked
    ``~/.scitex/agent-container/agents`` is followed into the real
    ``~/.dotfiles/...`` checkout) and asks git for the toplevel. Returns
    ``None`` when the path is not inside a git working tree or git is
    unavailable.
    """
    p = Path(spec_path)
    # Resolve through symlinks so ``git -C`` sees the real checkout, not
    # the symlink-into-dotfiles. ``strict=False`` tolerates a path that
    # doesn't fully exist yet (defensive — caller passes a real file).
    try:
        resolved = p.resolve()
    except OSError:  # stx-allow: fallback (reason: a broken symlink in the spec path must degrade to "no repo", never crash the launch)
        return None
    start = resolved if resolved.is_dir() else resolved.parent
    try:
        proc = _run_git(start, "rev-parse", "--show-toplevel", timeout=5)
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: git missing / unusable → no repo, warn-and-continue)
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top) if top else None


def _upstream_ref(repo: Path) -> str | None:
    """Return the upstream tracking ref of the current branch, or None.

    e.g. ``origin/develop``. ``None`` when no upstream is configured —
    a detached HEAD or a branch with no ``@{upstream}`` — in which case
    drift is undefined and we report UNREACHABLE.
    """
    try:
        proc = _run_git(
            repo,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
            timeout=5,
        )
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: git unusable → treat as no upstream, warn-and-continue)
        return None
    if proc.returncode != 0:
        return None
    ref = proc.stdout.strip()
    return ref or None


def _fetch_cache_path() -> Path:
    """Host-stable cache file for last-fetch timestamps per repo.

    Uses the user-scope runtime dir (NOT project-scope) so the cache is
    one-per-host regardless of which project directory the launch was
    invoked from.
    """
    # Import lazily so a missing scitex_config (optional dep elsewhere)
    # never breaks module import; sac depends on it but be defensive.
    from scitex_config._ecosystem import local_state

    return local_state.user_path("agent-container", "runtime", "drift-fetch.json")


def _load_fetch_cache(cache_path: Path) -> dict:
    """Read the per-repo last-fetch timestamp map; {} on any problem."""
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text())
    except (
        OSError,
        ValueError,
    ):  # stx-allow: fallback (reason: a corrupt cache must not break drift detection; treat as empty)
        return {}
    return data if isinstance(data, dict) else {}


def _save_fetch_cache(cache_path: Path, data: dict) -> None:
    """Persist the last-fetch timestamp map; swallow write errors."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, indent=2))
    except OSError:  # stx-allow: fallback (reason: read-only home / disk-full must not break a launch — the only cost is re-fetching next time)
        pass


def _maybe_fetch(repo: Path, *, now: float, ttl: int) -> bool:
    """Run ``git fetch`` for ``repo`` unless fetched within ``ttl`` seconds.

    Returns True when the remote is reachable (a fresh-cached fetch
    counts as reachable). Returns False when a fetch was attempted and
    failed (unreachable remote / timeout) — the caller maps that to
    UNREACHABLE. The cache is keyed on the resolved repo path.
    """
    cache_path = _fetch_cache_path()
    cache = _load_fetch_cache(cache_path)
    key = str(repo)
    last = cache.get(key)
    if isinstance(last, (int, float)) and (now - last) < ttl:
        return True  # fetched recently — assume still reachable
    try:
        proc = _run_git(repo, "fetch", "--quiet")
    except subprocess.TimeoutExpired:
        return False
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: git missing / unusable → unreachable, warn-and-continue)
        return False
    if proc.returncode != 0:
        return False
    cache[key] = now
    _save_fetch_cache(cache_path, cache)
    return True


def _count_rev_list(repo: Path, range_spec: str) -> int | None:
    """Return ``git rev-list --count <range_spec>`` as an int, or None."""
    try:
        proc = _run_git(repo, "rev-list", "--count", range_spec, timeout=5)
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: git unusable → cannot count, caller maps to UNREACHABLE)
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    try:
        return int(out)
    except ValueError:  # stx-allow: fallback (reason: non-numeric git output is malformed; treat as uncountable)
        return None


def check_spec_source_drift(
    spec_path: str | Path,
    *,
    do_fetch: bool = True,
    ttl: int = _FETCH_TTL_S,
    now_fn=time.time,
) -> DriftStatus:
    """Compute the drift of the git repo that contains ``spec_path``.

    Args:
        spec_path: Path to the agent spec.yaml (or its directory).
        do_fetch: When True (default) refresh the remote via a cached
            ``git fetch`` before comparing. Set False to compare against
            whatever the local repo already knows (used by the fleet
            check, which does its own remote refresh).
        ttl: Seconds a successful fetch stays fresh before re-fetching.
        now_fn: Injectable clock (real ``time.time``); tests pass a
            real callable returning a fixed value to exercise the TTL
            without sleeping.

    Returns:
        A :class:`DriftStatus`. NEVER raises — every failure mode maps
        to NOT_A_REPO or UNREACHABLE so a launch is never crashed.
    """
    repo = spec_source_repo(spec_path)
    if repo is None:
        return DriftStatus(
            state=DriftState.NOT_A_REPO,
            detail="spec source is not inside a git working tree",
        )
    upstream = _upstream_ref(repo)
    if upstream is None:
        return DriftStatus(
            state=DriftState.UNREACHABLE,
            repo=str(repo),
            detail="no upstream tracking branch configured",
        )
    if do_fetch and not _maybe_fetch(repo, now=now_fn(), ttl=ttl):
        return DriftStatus(
            state=DriftState.UNREACHABLE,
            repo=str(repo),
            upstream=upstream,
            detail="git fetch failed (offline / auth / timeout)",
        )
    behind = _count_rev_list(repo, f"HEAD..{upstream}")
    ahead = _count_rev_list(repo, f"{upstream}..HEAD")
    if behind is None or ahead is None:
        return DriftStatus(
            state=DriftState.UNREACHABLE,
            repo=str(repo),
            upstream=upstream,
            detail="could not compare HEAD with upstream",
        )
    if behind and ahead:
        state = DriftState.DIVERGED
    elif behind:
        state = DriftState.BEHIND
    elif ahead:
        state = DriftState.AHEAD
    else:
        state = DriftState.CURRENT
    return DriftStatus(
        state=state,
        behind=behind,
        ahead=ahead,
        repo=str(repo),
        upstream=upstream,
    )


def drift_warning_lines(
    status: DriftStatus, *, agent: str | None = None, refusing: bool = False
) -> list[str]:
    """Build the loud stderr lines for a drifted spec source.

    Returns an empty list when ``status`` is CURRENT (nothing to warn).
    NOT_A_REPO / UNREACHABLE produce a single soft note (drift unknown).
    BEHIND / AHEAD / DIVERGED produce a prominent banner that names the
    drift and the exact fix command.

    ``refusing`` switches the banner from WARNING to ERROR and adds the named
    override, so the message the operator reads matches what actually happened.
    A refusal that still says "WARNING" trains the reader to expect a launch.
    """
    if status.state is DriftState.CURRENT:
        return []

    who = f" for agent '{agent}'" if agent else ""
    if status.state in (DriftState.NOT_A_REPO, DriftState.UNREACHABLE):
        return [
            f"sac-drift: spec source{who} could not be drift-checked "
            f"({status.summary()}). Continuing.",
        ]

    repo = status.repo or "<spec-source-repo>"
    if status.state is DriftState.BEHIND:
        fix = f"git -C {repo} pull --ff-only"
        why = (
            f"the spec source is {status.behind} commit(s) BEHIND "
            f"{status.upstream} — you may be launching a STALE spec."
        )
    elif status.state is DriftState.AHEAD:
        fix = f"git -C {repo} push"
        why = (
            f"the spec source has {status.ahead} unpushed local commit(s) "
            f"(AHEAD of {status.upstream}) — this spec will NOT propagate "
            "to other hosts until pushed."
        )
    else:  # DIVERGED
        fix = f"git -C {repo} pull --ff-only   # then resolve, then  git -C {repo} push"
        why = (
            f"the spec source has DIVERGED from {status.upstream} "
            f"({status.ahead} ahead / {status.behind} behind) — it is both "
            "stale AND unpushed."
        )
    bar = "!" * 72
    lines = [
        bar,
        f"sac-drift {'ERROR' if refusing else 'WARNING'}{who}:",
        f"  {why}",
        f"  repo:     {repo}",
        f"  fix:      {fix}",
    ]
    if refusing:
        lines.append("  refusing to start — resolve the drift, or start anyway with:")
        lines.append(f"  override: {ALLOW_STALE_FLAG}   /   {ALLOW_STALE_ENV}=1")
    lines.append(bar)
    return lines


def warn_if_spec_source_drifted(
    spec_path: str | Path,
    *,
    agent: str | None = None,
    strict: bool = False,
    do_fetch: bool = True,
    stream=None,
) -> DriftStatus:
    """Check + emit the launch-time drift report to ``stream``.

    Always returns the computed :class:`DriftStatus`. When ``strict`` is
    True AND the local spec is STALE (BEHIND / DIVERGED — see
    :attr:`DriftStatus.is_stale`), raises :class:`SpecSourceDriftError` so
    the caller hard-blocks the launch.

    AHEAD does NOT block even under strict: the spec about to launch is the
    newest one that exists, it merely has not propagated. NOT_A_REPO /
    UNREACHABLE never block either — drift is *unknown* there, not present.

    ``stream=None`` (the default) routes the report through
    ``scitex-logging`` — level-tagged, module-stamped, mirrored to the
    rotating file — because a bare unprefixed line in lifecycle output is
    exactly the no-discipline print the operator called out on 2026-08-15
    (a ``sac-drift:`` line with no level sat between properly-tagged
    ``WARN:`` lines in ``sac-restart``'s output). scitex-logging emits to
    stderr, so ``capsys`` still observes it. Passing an EXPLICIT stream
    keeps the old raw-print contract — that seam is deliberate and some
    callers own their report's destination.

    NEVER raises for any reason other than the deliberate strict-mode
    block: the drift computation itself is fully guarded.
    """
    # stx-allow: fallback (reason: the drift check is a best-effort guard;
    # an unforeseen bug in it must NEVER crash a launch — degrade to a
    # silent "unknown" status and let the agent start)
    try:
        status = check_spec_source_drift(spec_path, do_fetch=do_fetch)
    except (
        Exception
    ) as exc:  # stx-allow: fallback (reason: see above — resilience is the contract)
        status = DriftStatus(
            state=DriftState.UNREACHABLE,
            detail=f"drift check raised {type(exc).__name__}: {exc}",
        )
    refusing = bool(strict and status.is_stale)
    lines = drift_warning_lines(status, agent=agent, refusing=refusing)
    if lines:
        if stream is None:
            log = get_logger(__name__)
            if refusing:
                emit = log.error
            elif status.state in (DriftState.NOT_A_REPO, DriftState.UNREACHABLE):
                # drift UNKNOWN, launch proceeds — informational, not a warning
                emit = log.info
            else:
                emit = log.warning
            for line in lines:
                emit(line)
        else:
            for line in lines:
                print(line, file=stream, flush=True)
    if refusing:
        raise SpecSourceDriftError(status, agent=agent)
    return status


class SpecSourceDriftError(RuntimeError):
    """Raised when the spec source is STALE and the start was not overridden.

    Carries the offending :class:`DriftStatus` so the CLI layer can set
    a clear non-zero exit. Only fires for BEHIND / DIVERGED — AHEAD,
    NOT_A_REPO and UNREACHABLE never escalate.
    """

    def __init__(self, status: DriftStatus, *, agent: str | None = None):
        self.status = status
        self.agent = agent
        who = f" for agent '{agent}'" if agent else ""
        super().__init__(
            f"sac-drift: spec source{who} is STALE ({status.summary()}); "
            f"refusing to launch a spec that may be out of date. Pull the "
            f"repo, or start anyway with {ALLOW_STALE_FLAG} / "
            f"{ALLOW_STALE_ENV}=1."
        )


__all__ = [
    "ALLOW_STALE_ENV",
    "ALLOW_STALE_FLAG",
    "SpecSourceDriftError",
    "check_spec_source_drift",
    "drift_warning_lines",
    "spec_source_repo",
    "warn_if_spec_source_drifted",
]
