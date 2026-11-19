"""Own one Hermes gateway and attach the official Ink TUI to it."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .._runners._hermes_owned_session import owned_session_projection
from ._hermes_hidden_launch import gateway_ready_dir, start_gateway
from ._hermes_tui_context_owner import (
    apply_session_age_policy,
    clear_session_heartbeat,
    fresh_command,
    make_session_observer,
    reconcile_command,
    requested_session,
    resume_command,
    transition_tui_child,
)
from ._hermes_tui_gateway_state import (  # noqa: F401  (re-exported)
    GATEWAY_FILE,
    GATEWAY_LOCK_FILE,
    READY_FILE,
    SUPERVISION_FILE,
    _atomic_json,
    _gateway_state_lock,
    _publish_gateway_state,
    _remove_owned_gateway_state,
    _select_owned_session,
    _terminate,
    _unlink_if_generation,
    _wait_for_port,
    _wait_for_readiness,
    _write_supervision,
)

POLL_SECONDS = 3.0
STARTUP_GRACE_SECONDS = 30.0
ABSENT_POLLS_BEFORE_RECOVERY = 2
MAX_RECOVERIES_PER_WINDOW = 3
RECOVERY_WINDOW_SECONDS = 300.0


def _supervise_tui(
    command: list[str],
    *,
    env: dict[str, str],
    state_dir: Path,
    gateway: Any,
    spawn: Callable[..., Any] = subprocess.Popen,
    active_list: Callable[[Path], list[dict]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    poll_s: float = POLL_SECONDS,
    startup_grace_s: float = STARTUP_GRACE_SECONDS,
    on_spawn: Callable[[Any], None] | None = None,
    on_session_attached: Callable[[dict], None] | None = None,
    on_session_observed: Callable[[dict], str | None] | None = None,
    recover_pending: Callable[[Path], str | None] | None = None,
    finalize_pending: Callable[[Path, dict], None] | None = None,
    recover_fresh: Callable[[Path], bool] | None = None,
    finalize_fresh: Callable[[Path, dict], None] | None = None,
) -> tuple[Any, int]:
    """Keep the official TUI attached to Hermes' authoritative live session.

    Hermes may detach a slow fanout peer without closing its websocket, leaving
    Ink stuck on ``computing``. It relaunches only an absent TUI child; a
    not-yet-observed continuation drops its startup query so recovery cannot
    replay a turn.
    """
    if active_list is None:
        from ._hermes_tui_rpc import active_sessions as active_list

    pending_fresh = recover_fresh(state_dir) if recover_fresh is not None else False
    pending_recovery = (
        recover_pending(state_dir) if recover_pending is not None else None
    )
    if pending_fresh and pending_recovery:
        raise RuntimeError("Hermes has conflicting pending completion and rotation")
    if pending_fresh:
        command = fresh_command(command, preserve_query=False)
    elif pending_recovery:
        command = resume_command(command, pending_recovery)
    session_mode, expected_identity = requested_session(command)
    tui = spawn(command, env=env)
    if on_spawn is not None:
        on_spawn(tui)
    generation_started = monotonic()
    absent_polls = 0
    resume_key = ""
    recoveries: deque[float] = deque()
    attached_session_prepared = False
    _write_supervision(state_dir, state="starting", recoveries=0)

    while gateway.poll() is None and tui.poll() is None:
        try:
            sessions = active_list(state_dir)
        except Exception as exc:
            # An observation failure is not evidence that the TUI detached.
            _write_supervision(
                state_dir,
                state="observation_unavailable",
                detail=str(exc),
                recoveries=len(recoveries),
            )
            sleep(poll_s)
            continue

        if recover_pending is not None and not pending_recovery:
            try:
                recovered = recover_pending(state_dir)
            except Exception as exc:
                _write_supervision(
                    state_dir,
                    state="context_gc_recovery_unavailable",
                    detail=str(exc),
                    active_sessions=len(sessions),
                    recoveries=len(recoveries),
                )
                sleep(poll_s)
                continue
            if recovered is not None:
                if recovered:
                    pending_recovery = recovered
                    (
                        tui,
                        command,
                        session_mode,
                        expected_identity,
                        resume_key,
                    ) = transition_tui_child(
                        tui=tui,
                        command=command,
                        replacement=recovered,
                        env=env,
                        spawn=spawn,
                        terminate=_terminate,
                        on_spawn=on_spawn,
                    )
                    generation_started = monotonic()
                    absent_polls = 0
                    attached_session_prepared = False
                # The active list predates journal reconciliation. Refresh it
                # before ownership selection after either commit or rollback.
                sleep(poll_s)
                continue

        if recover_fresh is not None and not pending_fresh:
            try:
                recovered_fresh = recover_fresh(state_dir)
            except Exception as exc:
                _write_supervision(
                    state_dir,
                    state="context_gc_recovery_unavailable",
                    detail=str(exc),
                    active_sessions=len(sessions),
                    recoveries=len(recoveries),
                )
                sleep(poll_s)
                continue
            if recovered_fresh:
                pending_fresh = True
                (
                    tui,
                    command,
                    session_mode,
                    expected_identity,
                    resume_key,
                ) = transition_tui_child(
                    tui=tui,
                    command=command,
                    replacement="",
                    env=env,
                    spawn=spawn,
                    terminate=_terminate,
                    on_spawn=on_spawn,
                )
                generation_started = monotonic()
                absent_polls = 0
                attached_session_prepared = False
                sleep(poll_s)
                continue

        try:
            owned_session = _select_owned_session(
                sessions,
                expected_identity=expected_identity,
                previous=resume_key,
                adopt_single=session_mode == "fresh",
            )
        except RuntimeError as exc:
            _write_supervision(
                state_dir,
                state="recovery_refused",
                detail=str(exc),
                active_sessions=len(sessions),
                recoveries=len(recoveries),
            )
            return tui, 70

        if owned_session is not None:
            absent_polls = 0
            if pending_fresh and finalize_fresh is not None:
                try:
                    finalize_fresh(state_dir, owned_session)
                except Exception as exc:
                    _write_supervision(
                        state_dir,
                        state="context_gc_recovery_unavailable",
                        detail=str(exc),
                        active_sessions=len(sessions),
                        recoveries=len(recoveries),
                    )
                    sleep(poll_s)
                    continue
                pending_fresh = False
            if pending_recovery and finalize_pending is not None:
                try:
                    finalize_pending(state_dir, owned_session)
                except Exception as exc:
                    _write_supervision(
                        state_dir,
                        state="context_gc_recovery_unavailable",
                        detail=str(exc),
                        active_sessions=len(sessions),
                        recoveries=len(recoveries),
                    )
                    sleep(poll_s)
                    continue
                pending_recovery = None
            from ._hermes_context_gc import OWNED_SESSION_FILE

            _atomic_json(
                state_dir / OWNED_SESSION_FILE,
                owned_session_projection(state_dir, owned_session),
            )
            resume_key = str(
                owned_session.get("session_key")
                or owned_session.get("id")
                or resume_key
            ).strip()
            if not attached_session_prepared and on_session_attached is not None:
                try:
                    on_session_attached(owned_session)
                except Exception as exc:
                    # The session stays live, but it is not declared attached
                    # until its model-calling periodic wakeup is proven absent.
                    # Retry on the next bounded observer tick.
                    _write_supervision(
                        state_dir,
                        state="periodic_turn_disable_unavailable",
                        detail=str(exc),
                        active_sessions=len(sessions),
                        stored_session_id=resume_key,
                        recoveries=len(recoveries),
                    )
                    sleep(poll_s)
                    continue
                attached_session_prepared = True
            replacement: str | None = None
            if on_session_observed is not None:
                try:
                    replacement = on_session_observed(owned_session)
                except Exception as exc:
                    _write_supervision(
                        state_dir,
                        state="context_gc_refused",
                        detail=str(exc),
                        active_sessions=len(sessions),
                        stored_session_id=resume_key,
                        recoveries=len(recoveries),
                    )
                    sleep(poll_s)
                    continue
            if replacement is not None:
                if replacement:
                    pending_recovery = replacement
                else:
                    pending_fresh = True
                _write_supervision(
                    state_dir,
                    state="context_gc_transition",
                    detail=(
                        "fresh session proven from durable handoff"
                        if replacement
                        else "task completed; starting next task fresh"
                    ),
                    recoveries=len(recoveries),
                )
                (
                    tui,
                    command,
                    session_mode,
                    expected_identity,
                    resume_key,
                ) = transition_tui_child(
                    tui=tui,
                    command=command,
                    replacement=replacement,
                    env=env,
                    spawn=spawn,
                    terminate=_terminate,
                    on_spawn=on_spawn,
                )
                generation_started = monotonic()
                absent_polls = 0
                attached_session_prepared = False
                sleep(poll_s)
                continue
            _write_supervision(
                state_dir,
                state="attached",
                active_sessions=len(sessions),
                stored_session_id=resume_key,
                periodic_turns="disabled",
                recoveries=len(recoveries),
            )
            sleep(poll_s)
            continue

        outside_grace = monotonic() - generation_started >= startup_grace_s
        if outside_grace:
            absent_polls += 1
        if absent_polls < ABSENT_POLLS_BEFORE_RECOVERY:
            sleep(poll_s)
            continue

        now = monotonic()
        while recoveries and now - recoveries[0] > RECOVERY_WINDOW_SECONDS:
            recoveries.popleft()
        if (not expected_identity and not resume_key) or len(
            recoveries
        ) >= MAX_RECOVERIES_PER_WINDOW:
            _write_supervision(
                state_dir,
                state="recovery_refused",
                detail=(
                    "a fresh TUI created no session, so recovery cannot replay "
                    "its startup turn safely"
                    if not expected_identity and not resume_key
                    else "TUI transport recovery budget exhausted"
                ),
                recoveries=len(recoveries),
            )
            return tui, 70

        recoveries.append(now)
        _write_supervision(
            state_dir,
            state="recovering",
            detail=(
                "configured Hermes session is absent; reconciling official TUI "
                "without replaying its startup turn"
            ),
            **({"stored_session_id": resume_key} if resume_key else {}),
            recoveries=len(recoveries),
        )
        _terminate(tui)
        recovery_command = (
            resume_command(command, resume_key)
            if resume_key
            else reconcile_command(command)
        )
        tui = spawn(recovery_command, env=env)
        if on_spawn is not None:
            on_spawn(tui)
        generation_started = monotonic()
        absent_polls = 0
        attached_session_prepared = False

    return tui, int(tui.poll() or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sac-hermes-tui-owner")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--max-session-age-minutes", type=int, default=4320)
    # Sealed runs: paths (state dir, SAC installations) the agent tree must
    # not see; see runtimes/_hermes_hidden_launch and _sandbox/_hide.
    parser.add_argument("--hide", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a Hermes TUI command is required after --")

    state_dir = Path(args.state_dir)
    hidden = list(args.hide)
    generation = uuid.uuid4().hex
    ready_dir = gateway_ready_dir(state_dir, hidden=hidden)
    gateway_ready_path = ready_dir / f".{READY_FILE}.{generation}"
    token_path = state_dir / "hermes-api.key"
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise RuntimeError(f"missing or invalid Hermes gateway key: {token_path}")
    gateway_ready_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env["HERMES_DESKTOP_READY_FILE"] = str(gateway_ready_path)
    gateway, spawn = start_gateway(env=env, token=token, hidden=hidden)
    tui: subprocess.Popen | None = None

    def remember_tui(process: subprocess.Popen) -> None:
        nonlocal tui
        tui = process

    def forward(signum: int, _frame: object) -> None:
        if tui is not None and tui.poll() is None:
            tui.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        port = _wait_for_port(gateway_ready_path, gateway)
        _wait_for_readiness(port, token, gateway)
        _publish_gateway_state(
            state_dir,
            generation=generation,
            port=port,
            gateway_pid=gateway.pid,
        )
        tui_env = os.environ.copy()
        tui_env["HERMES_TUI_GATEWAY_URL"] = (
            f"ws://127.0.0.1:{port}/api/ws?token={token}"
        )
        from ._hermes_context_gc import complete_pending_completion
        from ._hermes_context_rpc import (
            complete_pending_transition,
            reconcile_pending_completion,
            reconcile_pending_transition,
            stored_session_for_identity,
        )

        command = apply_session_age_policy(
            command,
            state_dir=state_dir,
            max_age_minutes=args.max_session_age_minutes,
            stored_lookup=stored_session_for_identity,
        )

        tui, result = _supervise_tui(
            command,
            env=tui_env,
            state_dir=state_dir,
            gateway=gateway,
            spawn=spawn,
            on_spawn=remember_tui,
            on_session_attached=lambda session: clear_session_heartbeat(
                state_dir, session
            ),
            on_session_observed=make_session_observer(state_dir, command),
            recover_pending=reconcile_pending_transition,
            finalize_pending=complete_pending_transition,
            recover_fresh=reconcile_pending_completion,
            finalize_fresh=complete_pending_completion,
        )
        return result
    finally:
        from ._hermes_context_gc import OWNED_SESSION_FILE

        _unlink_if_generation(state_dir / OWNED_SESSION_FILE, generation)
        _remove_owned_gateway_state(state_dir, generation=generation)
        gateway_ready_path.unlink(missing_ok=True)
        if ready_dir != state_dir:
            shutil.rmtree(ready_dir, ignore_errors=True)
        _write_supervision(state_dir, state="stopped")
        if tui is not None and tui.poll() is None:
            tui.terminate()
            try:
                tui.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tui.kill()
        if gateway.poll() is None:
            gateway.terminate()
            try:
                gateway.wait(timeout=5)
            except subprocess.TimeoutExpired:
                gateway.kill()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["GATEWAY_FILE", "READY_FILE", "main"]
