"""Own one opencode gateway and attach the official TUI to it.

The Hermes-owner shape at pilot scope: start ``opencode serve`` as a
child, wait for ``GET /global/health``, resolve the stable
``sac:<agent>`` session (reusing the mapped one when the server still
holds it), publish ``opencode-serve.json`` ``{url}`` + the session map
atomically (``0600``) so host-side SAC (turn bridge, tail, heartbeat)
and the driver address the SAME session the TUI is viewing, then run
the TUI as the supervised foreground child with ``--session`` pinned
(``opencode attach`` otherwise opens an empty view that never shows
HTTP-submitted turns — observed on the canary 2026-09-29). Delivery
never types into the terminal — SAC talks to the server over HTTP. On
exit the serve state is removed and both children reaped.

Deliberately leaner than ``_hermes_tui_owner``: no session-age GC, no
recovery monitors, no generation fencing (single owner per state dir
per tmux session). Those graduate with the pilot.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from ._gateway_opencode import SERVE_FILE, SESSION_MAP_FILE

_HEALTH_TIMEOUT_S = 30.0


def _atomic_json(path: Path, value: dict) -> None:
    """Write ``value`` atomically with owner-only permissions."""
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(value, separators=(",", ":")).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _wait_for_health(url: str, process: subprocess.Popen, timeout_s: float) -> None:
    """Poll ``GET <url>/global/health`` until 200 or timeout."""
    deadline = time.monotonic() + timeout_s
    last_error = "no health observation"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Opencode serve exited before ready (rc={process.returncode})"
            )
        try:
            with urllib.request.urlopen(url + "/global/health", timeout=1.0) as resp:
                if 200 <= resp.status < 300:
                    return
                last_error = f"health answered HTTP {resp.status}"
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.1)
    raise RuntimeError(
        f"Opencode serve at {url} did not become ready within {timeout_s:g}s: "
        f"{last_error}"
    )


def _terminate(process: subprocess.Popen | None, *, timeout_s: float = 5.0) -> None:
    """Terminate + reap one child, escalating to kill on timeout."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()


def _read_session_map(state_dir: Path) -> dict:
    """Return the persisted ``sac:<agent>`` -> ``ses_*`` map, if any."""
    try:
        known = json.loads((state_dir / SESSION_MAP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return known if isinstance(known, dict) else {}


def _session_exists(url: str, session_id: str, timeout_s: float = 5.0) -> bool:
    """True when the server still holds ``session_id``."""
    request = urllib.request.Request(f"{url}/session/{session_id}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _create_session(url: str, key: str, timeout_s: float = 10.0) -> str:
    """Create the stable session titled ``key``; return its ``ses_*`` id."""
    body = json.dumps({"title": key}).encode()
    request = urllib.request.Request(
        f"{url}/session",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"Opencode serve would not create {key!r}: {exc}") from exc
    session_id = payload.get("id") if isinstance(payload, dict) else None
    if not session_id:
        raise RuntimeError(f"Opencode serve returned a malformed session: {payload!r}")
    return str(session_id)


def _resolve_stable_session(url: str, state_dir: Path, agent_name: str) -> str:
    """Return the ``ses_*`` id for ``sac:<agent>``, creating it once.

    The id is persisted in ``SESSION_MAP_FILE`` so the driver
    (``_gateway_opencode._resolve_session``) reuses the exact session
    the TUI is viewing instead of opening a second one.
    """
    key = f"sac:{agent_name}"
    known = _read_session_map(state_dir)
    session_id = known.get(key)
    if session_id and _session_exists(url, str(session_id)):
        return str(session_id)
    session_id = _create_session(url, key)
    _atomic_json(state_dir / SESSION_MAP_FILE, {**known, key: session_id})
    return session_id


def _pin_attach_session(command: list[str], session_id: str) -> list[str]:
    """Pin ``--session`` on an ``opencode attach`` command, if absent.

    The argv declares the intent (attach to this server); the owner
    binds the stable session, which only exists once serve is healthy.
    A caller-supplied ``--session``/``-s`` always wins.
    """
    if (
        len(command) >= 2
        and command[0] == "opencode"
        and command[1] == "attach"
        and "--session" not in command
        and "-s" not in command
    ):
        return [*command, "--session", session_id]
    return command


def main(argv: list[str] | None = None) -> int:
    """Start the owned serve gateway, publish it, supervise the TUI."""
    parser = argparse.ArgumentParser(prog="sac-opencode-tui-owner")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument(
        "--agent-name",
        default="",
        help="Agent owning the stable sac:<name> session. Defaults to the "
        "state-dir basename (which IS the agent name for the "
        "/state/<name> container mount); pass explicitly for host runs "
        "whose state dir is not named after the agent.",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a TUI attach command is required after --")

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    port = int(args.port)
    if not 0 < port < 65536:
        parser.error(f"invalid --port {args.port!r}")
    url = f"http://127.0.0.1:{port}"
    serve_path = state_dir / SERVE_FILE
    # Inside the container the state dir mounts at /state/<name>;
    # on a host-run canary it is a real path. Prefer the explicit
    # identity; fall back to the basename convention.
    agent_name = args.agent_name.strip() or state_dir.name

    serve = subprocess.Popen(
        ["opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1"],
    )
    tui: subprocess.Popen | None = None

    def forward(signum: int, _frame: object) -> None:
        if tui is not None and tui.poll() is None:
            tui.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    try:
        _wait_for_health(url, serve, _HEALTH_TIMEOUT_S)
        _atomic_json(serve_path, {"url": url})
        session_id = _resolve_stable_session(url, state_dir, agent_name)
        tui = subprocess.Popen(_pin_attach_session(command, session_id))
        return int(tui.wait())
    finally:
        serve_path.unlink(missing_ok=True)
        _terminate(tui)
        _terminate(serve)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
