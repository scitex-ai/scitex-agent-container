"""Post-launch verification for ``sac agents start`` — did it actually COME UP?

WHAT THIS FIXES (v4 migration step 1 — ships alone, before any refactor)
------------------------------------------------------------------------
``sac agents start`` printed its green SUCC the moment the launch command
returned, and for the detached runtimes that moment proves almost nothing:
``ApptainerContainerRuntime.start`` returns as soon as ``apptainer exec``
is backgrounded and its pid file is written. An agent that died seconds
later — an a2a port bind collision (``OSError: [Errno 98]``), a
``VenvDistributionError`` boot refusal, the SDK's 1MiB stdio frame kill;
all three real, all three server-side deaths behind a SUCC on 2026-08-14 —
was reported as started, exit 0. Operator directive, verbatim:
「そういう場合には必ずエラーを caller 側に出さないとだめです」 and
「エラーが握りつぶされないこと…エラーがどこで起こったかを示せるととても
よいです」.

THE EVIDENCE, PER PATH (only signals that are actually WRITTEN today)
---------------------------------------------------------------------
* SDK / ``claude-agent-sdk`` (and every other non-TUI runtime): the
  in-container runner writes ``<state>/heartbeat.json`` synchronously at
  boot — ``STATE_STARTING`` first, wall-clock ``ts``, through the
  ``/state`` bind (``_runners/claude_session.py::run``; the same
  ``write_heartbeat`` call also lands the ``state.db.heartbeats`` diary
  row — the JSON file is the documented local fast path for exactly this
  kind of read). A beat whose ``ts`` is at/after the moment we launched is
  POSITIVE evidence a NEW incarnation booted. A beat with
  ``state == "stopping"`` is excluded: on a ``--force`` cycle the OLD
  incarnation's farewell beat can also land after our launch timestamp
  and must not vouch for the new one.
* TUI: ``TuiSessionRuntime.start`` already blocks through its boot drain
  and only returns True once the session survived boot (session alive +
  input-ready observed, or the liveness probe when the drain is off).
  No runner-written heartbeat exists on this path — TUI beats come from
  the listen daemon's centralized loop, which may not be running — so the
  genuine post-start signal is the runtime's own liveness probe (tmux
  pane process alive). We re-probe once instead of waiting 90s for a
  beat nobody writes.

THREE-VALUED, NEVER BINARY (fleet constitution)
-----------------------------------------------
The verdict distinguishes:

* ``verified-up``     — positive evidence observed; the ONLY status that
  may be reported as a green "started".
* ``verified-failed`` — the launched process is GONE; the boot log tail
  (the real error text, e.g. the Errno 98 line) rides on the verdict so
  it reaches the caller's terminal, and the file it was read from is
  named so the operator can go deeper.
* ``unverified``      — the window expired with the process still
  standing but nothing vouching for it. NOT collapsed into either pole:
  it exits non-zero like a failure, but its wording says "could not
  verify" and names where to look, never "failed".
* ``skipped``         — verification did not apply (window disabled,
  foreground/one-shot, or the start was brokered to another host where
  the evidence lives). Reported honestly, never as SUCC-with-evidence.

The window is configurable: ``--verify-window`` on ``sac agents start``
(transported via ``SAC_START_VERIFY_WINDOW_S`` so the parallel re-exec
path inherits it), default 90s, ``0`` disables.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_VERIFY_WINDOW_S",
    "SKIPPED",
    "UNVERIFIED",
    "VERIFIED_FAILED",
    "VERIFIED_UP",
    "VERIFY_WINDOW_ENV",
    "LaunchVerdict",
    "resolve_verify_window",
    "verify_launch",
]

# Verdict statuses — the closed vocabulary callers branch on.
VERIFIED_UP = "verified-up"
VERIFIED_FAILED = "verified-failed"
UNVERIFIED = "unverified"
SKIPPED = "skipped"

#: Default bounded wait for launch evidence. Generous enough for a cold
#: apptainer boot + venv checks + the runner's first heartbeat on a
#: loaded host; short enough that a stillborn launch is reported while
#: the operator is still looking at the terminal.
DEFAULT_VERIFY_WINDOW_S = 90.0

#: Env transport for the window (the ``--verify-window`` click callback
#: sets it; the parallel per-agent re-exec inherits it). ``0`` disables.
VERIFY_WINDOW_ENV = "SAC_START_VERIFY_WINDOW_S"

DEFAULT_POLL_INTERVAL_S = 1.0

#: Bound on the boot-log tail carried on a verdict — mirrors the 4000
#: chars ``_start_failure_diag._format_boot_stderr_section`` uses, so
#: the two failure reports show the same amount of context.
_TAIL_CHARS = 4_000

#: The old incarnation's farewell state — never evidence of a NEW boot.
_STATE_STOPPING = "stopping"


@dataclass(frozen=True)
class LaunchVerdict:
    """The post-launch verdict plus the evidence it was drawn from.

    ``status`` is one of the four module constants; ``evidence`` is the
    operator-facing sentence naming WHAT was observed (or not);
    ``log_path``/``log_tail`` carry the boot log that explains a failure
    (the file is NAMED so the operator can go deeper than the tail);
    ``heartbeat_path`` names the evidence file the wait was watching
    (``None`` on the TUI path, which has no runner-written heartbeat);
    ``waited_s`` is how long the verdict took to reach.
    """

    status: str
    evidence: str
    log_path: str | None
    log_tail: str
    waited_s: float
    heartbeat_path: str | None

    @property
    def ok(self) -> bool:
        """True iff this verdict must NOT flip the caller's exit code."""
        return self.status in (VERIFIED_UP, SKIPPED)

    def as_dict(self) -> dict:
        """``--json`` envelope fields (flat, self-describing)."""
        return {
            "status": self.status,
            "evidence": self.evidence,
            "boot_log": self.log_path,
            "boot_log_tail": self.log_tail,
            "heartbeat_file": self.heartbeat_path,
            "waited_s": round(self.waited_s, 1),
        }


def resolve_verify_window(explicit: float | None = None) -> float:
    """Resolve the verification window: explicit arg > env var > default.

    A malformed env value FAILS LOUD (``ValueError`` naming the variable
    and the bad text) rather than silently falling back — a typo'd
    window must not quietly become 90s of unexpected blocking, nor
    quietly disable verification.
    """
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(VERIFY_WINDOW_ENV, "").strip()
    if not raw:
        return DEFAULT_VERIFY_WINDOW_S
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"{VERIFY_WINDOW_ENV}={raw!r} is not a number of seconds. "
            f"Use e.g. {VERIFY_WINDOW_ENV}=90 (or 0 to disable launch "
            "verification)."
        )


def _resolve_state_dir(config: Any) -> Path:
    """The agent's host-side state dir — SAME resolution the runtimes use.

    ``_runners._session_state.state_dir_for`` rooted at the project-scope
    ``runtime/`` when the spec lives in one (mirrors
    ``ApptainerContainerRuntime._state_dir`` / ``tui_session.
    state_dir_for_config``), else the home-scope default.
    """
    from ..runtimes._provider_common import project_runtime_root
    from .._runners._session_state import state_dir_for

    return state_dir_for(config.name, root=project_runtime_root(config))


def _is_tui_runtime(runtime: Any) -> bool:
    """True iff ``runtime`` is the interactive TUI runtime.

    Decided on the runtime OBJECT (the same dispatch ``_get_runtime``
    performed), not on ``config.runtime`` — ``provider: openai`` selects
    the OpenAI runtime regardless of the runtime string, and an empty
    runtime string means TUI only in the Claude family.
    """
    try:
        from ..runtimes.tui_session import TuiSessionRuntime
    except Exception:  # stx-allow: fallback (reason: a partial install without the tmux stack cannot be running TUI agents — the generic heartbeat path is the honest probe)
        return False
    return isinstance(runtime, TuiSessionRuntime)


def _first_existing(state_dir: Path, names: tuple[str, ...]) -> Path | None:
    """First boot-log candidate that exists on disk, else ``None``."""
    for name in names:
        candidate = state_dir / name
        if candidate.is_file():
            return candidate
    return None


def _tail(path: Path | None) -> str:
    """Bounded tail of ``path`` — empty (never raising) when unreadable."""
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(errors="replace")[-_TAIL_CHARS:].rstrip()
    except OSError:  # stx-allow: fallback (reason: an unreadable log must degrade to "no tail", not mask the launch verdict itself)
        return ""


def _iso(ts: float) -> str:
    """Wall-clock heartbeat ``ts`` as a local-timezone ISO stamp."""
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _runtime_reports_running(runtime: Any, config: Any) -> bool:
    """The runtime's own liveness probe — a RAISING probe is UNKNOWN.

    A probe that could not run must not convict (the false-RED lesson of
    :mod:`._start_verdict`): treat it as "still possibly alive" so the
    wait continues and the window expiry reports UNVERIFIED, never a
    fabricated death.
    """
    try:
        return bool(runtime.is_running(config))
    except Exception:  # stx-allow: fallback (reason: a probe that cannot run is UNKNOWN liveness, not evidence of death — see _start_verdict's false-RED rationale)
        return True


def verify_launch(
    config: Any,
    runtime: Any | None = None,
    *,
    launched_at: float,
    window_s: float | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    state_dir: Path | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> LaunchVerdict:
    """Wait (bounded) for evidence the just-launched agent actually came up.

    ``launched_at`` is the caller's wall-clock stamp taken BEFORE the
    launch — freshness of a heartbeat is judged against it. ``runtime``
    and ``state_dir`` are injection seams defaulting to the real
    ``_get_runtime(config)`` dispatch and the runtimes' shared state-dir
    resolution; ``sleep_fn``/``now_fn`` let the suite drive the wait
    deterministically with real time values.

    Never raises on the evidence paths — every outcome is a verdict.
    """
    window = resolve_verify_window(window_s)
    if window <= 0:
        return LaunchVerdict(
            SKIPPED,
            f"launch verification disabled ({VERIFY_WINDOW_ENV}={window:g})",
            None,
            "",
            0.0,
            None,
        )
    if runtime is None:
        from ._runtime_select import _get_runtime

        runtime = _get_runtime(config)
    if state_dir is None:
        state_dir = _resolve_state_dir(config)

    if _is_tui_runtime(runtime):
        return _verify_tui(config, runtime, state_dir, launched_at, now_fn)
    return _verify_by_heartbeat(
        config,
        runtime,
        state_dir,
        launched_at=launched_at,
        window=window,
        poll_interval_s=poll_interval_s,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
    )


def _verify_tui(
    config: Any,
    runtime: Any,
    state_dir: Path,
    launched_at: float,
    now_fn: Callable[[], float],
) -> LaunchVerdict:
    """TUI verdict — one liveness re-probe after the runtime's boot drain.

    ``TuiSessionRuntime.start`` already refused to return True unless the
    session survived boot; this probe is the second witness at report
    time, and its failure reading points at ``boot.stderr.log`` (where
    the runtime redirects the inner ``apptainer exec … claude`` stderr).
    """
    waited = max(0.0, now_fn() - launched_at)
    log_path = _first_existing(state_dir, ("boot.stderr.log", "stdout.log"))
    if _runtime_reports_running(runtime, config):
        return LaunchVerdict(
            VERIFIED_UP,
            "ready: boot drain passed and the tmux pane process is alive "
            f"(probed {waited:.1f}s after launch)",
            str(log_path) if log_path else None,
            "",
            waited,
            None,
        )
    return LaunchVerdict(
        VERIFIED_FAILED,
        f"tmux session/pane is GONE {waited:.1f}s after launch "
        "(runtime.is_running -> False) — the inner claude did not survive "
        "boot",
        str(log_path) if log_path else None,
        _tail(log_path),
        waited,
        None,
    )


def _verify_by_heartbeat(
    config: Any,
    runtime: Any,
    state_dir: Path,
    *,
    launched_at: float,
    window: float,
    poll_interval_s: float,
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], float],
) -> LaunchVerdict:
    """SDK-path verdict — poll for a FRESH heartbeat from a NEW incarnation.

    Per tick, in evidence order: (1) a beat stamped at/after
    ``launched_at`` (and not the old run's ``stopping`` farewell) proves
    the new runner booted → VERIFIED_UP; (2) the runtime's own pid probe
    reporting the container DEAD is definitive → VERIFIED_FAILED with the
    boot-log tail (``stdout.log`` — apptainer merges the runner's stderr
    into it, so the Errno 98 / VenvDistributionError text lives there);
    (3) window expiry with the process still standing → UNVERIFIED, which
    is exit-nonzero but worded as "could not verify", never "failed".
    """
    from .._runners._session_state import read_heartbeat

    heartbeat_path = state_dir / "heartbeat.json"
    log_candidates = ("stdout.log", "boot.stderr.log")
    deadline = launched_at + window
    while True:
        beat = read_heartbeat(state_dir)
        if beat is not None:
            ts = beat.get("ts")
            state = beat.get("state")
            if (
                isinstance(ts, (int, float))
                and float(ts) >= launched_at
                and state != _STATE_STOPPING
            ):
                waited = max(0.0, now_fn() - launched_at)
                return LaunchVerdict(
                    VERIFIED_UP,
                    f"ready: first fresh heartbeat at {_iso(float(ts))} "
                    f"(state={state}, pid {beat.get('pid')}), "
                    f"{waited:.1f}s after launch",
                    None,
                    "",
                    waited,
                    str(heartbeat_path),
                )
        if not _runtime_reports_running(runtime, config):
            waited = max(0.0, now_fn() - launched_at)
            log_path = _first_existing(state_dir, log_candidates)
            return LaunchVerdict(
                VERIFIED_FAILED,
                f"container process is DEAD {waited:.1f}s after launch "
                "(runtime.is_running -> False) and no fresh heartbeat was "
                "ever written",
                str(log_path) if log_path else None,
                _tail(log_path),
                waited,
                str(heartbeat_path),
            )
        if now_fn() >= deadline:
            waited = max(0.0, now_fn() - launched_at)
            log_path = _first_existing(state_dir, log_candidates)
            return LaunchVerdict(
                UNVERIFIED,
                f"no fresh heartbeat within {window:g}s — the container "
                "process still reports running, but nothing proves the "
                "agent came up",
                str(log_path) if log_path else None,
                _tail(log_path),
                waited,
                str(heartbeat_path),
            )
        sleep_fn(poll_interval_s)
