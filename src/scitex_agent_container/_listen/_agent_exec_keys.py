"""``type: key`` handler for ``POST /agents/<name>/send`` (listen).

Split out of :mod:`._agent_exec` (which is at its per-file line cap) so
the key-passthrough logic lives in one cohesive place. Two routes:

  * cancel keys (ESC / C-c / SIGINT) → SIGINT the runner pid, which
    interrupts the current turn without killing the agent. This is the
    historical behaviour, preserved verbatim.
  * every OTHER named key / sequence (Enter, Up, Down, Tab, digits, …)
    → ``tmux send-keys`` into the agent's live tmux session so the
    keystroke lands in the TUI exactly as if typed.

Validation against the tmux key vocabulary (:mod:`..._runners._tmux._keys`)
fails loud with the valid set listed; nothing is half-delivered.
"""

from __future__ import annotations

import os
from typing import Callable, Tuple

from starlette.responses import JSONResponse, Response

from .._runners._session_state import state_dir_for
from .._runners._tmux._keys import (
    UnknownKeyError,
    parse_key_sequence,
    validate_keys,
)

__all__ = ["_handle_key_send", "_CANCEL_KEYS"]

# Cancel keys keep their historical interrupt semantics — SIGINT the
# runner pid rather than typing the literal key into the TUI.
_CANCEL_KEYS = frozenset({"ESC", "C-c", "SIGINT"})

# Injection seam: resolve ``name`` → ``(session_name, mux)`` where mux
# exposes ``exists(session)`` + ``send_keys(session, *keys)``. The
# default goes through the real config + multiplexer; tests pass a
# recording fake so the send-keys branch is exercised without a live
# tmux session (mirrors the existing tmux-test injection pattern).
MuxResolver = Callable[[str], Tuple[str, object]]


def _default_mux_resolver(name: str) -> Tuple[str, object]:
    """Resolve ``name`` to its tmux ``(screen_name, multiplexer)``."""
    from ..config import load_config
    from ..config._resolve import resolve_config
    from .._runners._tmux.multiplexer import get_multiplexer

    spec_path = resolve_config(name)
    cfg = load_config(spec_path)
    return cfg.screen_name, get_multiplexer(cfg)


def _interrupt_pid(name: str) -> Response:
    """SIGINT the agent's recorded runner pid (cancel the current turn)."""
    import signal as _signal

    sd = state_dir_for(name)
    pid_file = sd / "pid"
    if not pid_file.is_file():
        return JSONResponse(
            {"error": f"agent {name!r} has no live session"},
            status_code=404,
        )
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, _signal.SIGINT)
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(
        {
            "name": name,
            "route": "interrupt",
            "pid": pid,
            "signal": "SIGINT",
        }
    )


def _send_keys_to_session(
    name: str,
    tmux_keys: list[str],
    *,
    mux_resolver: MuxResolver = _default_mux_resolver,
) -> Response:
    """Deliver validated tmux key names into the agent's tmux session.

    Resolves the agent's ``screen_name`` (its tmux session) + its
    multiplexer via ``mux_resolver`` and routes the keys through the
    multiplexer's ``send_keys`` — the same primitive the TUI runner
    uses. Loud 404 when the spec cannot be resolved (unknown agent) or
    there is no live tmux session.
    """
    try:
        session, mux = mux_resolver(name)
    except Exception as exc:  # stx-allow: fallback (reason: unknown agent → 404 with reason, not ASGI 500)
        return JSONResponse({"error": str(exc)}, status_code=404)

    if not mux.exists(session):  # type: ignore[attr-defined]
        return JSONResponse(
            {
                "error": (
                    f"agent {name!r} has no live tmux session {session!r}"
                )
            },
            status_code=404,
        )
    mux.send_keys(session, *tmux_keys)  # type: ignore[attr-defined]
    return JSONResponse(
        {
            "name": name,
            "route": "send-keys",
            "session": session,
            "keys": tmux_keys,
        }
    )


def _handle_key_send(
    name: str,
    body: dict,
    *,
    mux_resolver: MuxResolver = _default_mux_resolver,
) -> Response:
    """Route a ``type: key`` body to SIGINT (cancel) or tmux send-keys.

    Body shapes:
        {"type":"key","key":"ESC"}             → SIGINT (cancel turn)
        {"type":"key","key":"Enter"}           → send-keys "Enter"
        {"type":"key","key":"1"}               → send-keys "1"
        {"type":"key","keys":"Up Up Enter"}    → send-keys Up Up Enter

    A single cancel ``key`` keeps the historical interrupt path. Any
    other ``key`` or any ``keys`` sequence is validated against the
    tmux vocabulary and delivered to the agent's tmux session.
    Unknown names → 400 with the valid set listed (no partial send).
    """
    key = body.get("key")
    keys = body.get("keys")

    if isinstance(key, str) and key in _CANCEL_KEYS and keys is None:
        return _interrupt_pid(name)

    if isinstance(keys, str):
        tokens = parse_key_sequence(keys)
    elif isinstance(key, str):
        tokens = [key]
    else:
        return JSONResponse(
            {"error": "key send requires a 'key' or 'keys' string"},
            status_code=400,
        )

    try:
        tmux_keys = validate_keys(tokens)
    except (UnknownKeyError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return _send_keys_to_session(name, tmux_keys, mux_resolver=mux_resolver)
