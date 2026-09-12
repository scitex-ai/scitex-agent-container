"""Own one Hermes gateway and attach the official Ink TUI to it."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

GATEWAY_FILE = "hermes-tui-gateway.json"
READY_FILE = "hermes-tui-gateway.ready.json"


def _wait_for_port(
    path: Path, process: subprocess.Popen, timeout_s: float = 30.0
) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Hermes gateway exited before ready (rc={process.returncode})"
            )
        try:
            port = int(json.loads(path.read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError):
            time.sleep(0.05)
            continue
        if 0 < port < 65536:
            return port
    raise RuntimeError(f"Hermes gateway did not publish {path} within {timeout_s:g}s")


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(value, separators=(",", ":")).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sac-hermes-tui-owner")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a Hermes TUI command is required after --")

    state_dir = Path(args.state_dir)
    ready_path = state_dir / READY_FILE
    descriptor_path = state_dir / GATEWAY_FILE
    token_path = state_dir / "hermes-api.key"
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise RuntimeError(f"missing or invalid Hermes gateway key: {token_path}")
    ready_path.unlink(missing_ok=True)
    descriptor_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env["HERMES_DASHBOARD_SESSION_TOKEN"] = token
    env["HERMES_DESKTOP_READY_FILE"] = str(ready_path)
    gateway = subprocess.Popen(
        ["hermes", "serve", "--host", "127.0.0.1", "--port", "0", "--isolated"],
        env=env,
    )
    tui: subprocess.Popen | None = None
    try:
        port = _wait_for_port(ready_path, gateway)
        _atomic_json(descriptor_path, {"port": port, "pid": gateway.pid})
        tui_env = os.environ.copy()
        tui_env["HERMES_TUI_GATEWAY_URL"] = (
            f"ws://127.0.0.1:{port}/api/ws?token={token}"
        )
        tui = subprocess.Popen(command, env=tui_env)

        def forward(signum: int, _frame: object) -> None:
            if tui is not None and tui.poll() is None:
                tui.send_signal(signum)

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        return tui.wait()
    finally:
        descriptor_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
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
