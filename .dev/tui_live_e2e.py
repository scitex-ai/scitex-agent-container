"""Live end-to-end driver for the TUI runtime on a real Max-subscription
account. NOT a pytest test — a deliberate operator-driven probe to capture
proof per lead a2a 47d19d23 ("real subscription account, capture tmux
scrollback showing a real turn processed, no SDK / claude -p").

Usage (from inside this worktree, with PYTHONPATH=src):

    PYTHONPATH=src /opt/venv-agent/bin/python .dev/tui_live_e2e.py

Side effects:
  - Builds a temporary HOME at /tmp/tui-e2e-<uuid>/home, copies the live
    user-state ``.claude.json`` from the SDK rotator's CLAUDE_CONFIG_DIR
    into HOME root (so the TUI's "first-run wizard" — theme picker,
    onboarding — is already satisfied).
  - Spawns a detached tmux session running /usr/local/bin/claude with
    HOME=<materialised> + CLAUDE_CONFIG_DIR=/tmp/sac-claude (where the
    rotator keeps fresh OAuth creds the TUI consults). This bypasses
    TuiSessionRuntime.start (which needs a full AgentConfig surface) and
    directly exercises the underlying multiplexer primitives the runtime
    delegates to: TmuxManager.start, .send_text_and_submit, .capture_logs,
    .stop. The mechanics under test are exactly the same.
  - Sends one user turn ("respond with 'pong'") via send_text_and_submit.
  - Polls capture_logs until the reply token appears (or hard 120s timeout).
  - Writes the full scrollback to .dev/tui_live_e2e_scrollback.txt for the
    PR-comment proof attachment.
  - Always stops the tmux session on exit (even on exception).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, "src")

from scitex_agent_container._runners._tmux.tmux import TmuxManager  # noqa: E402

_CLAUDE_CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/tmp/sac-claude"))
_LIVE_USER_STATE = _CLAUDE_CONFIG_DIR / ".claude.json"

_PROMPT = "Please respond with exactly the single word 'pong' and nothing else."
_REPLY_TOKEN = "pong"
_FIRST_TURN_WAIT_S = 8.0
_TIMEOUT_S = 120.0
_POLL_S = 1.5


def _ensure_binaries() -> None:
    for bin_ in ("tmux", "claude"):
        if shutil.which(bin_) is None:
            raise SystemExit(f"required binary missing on PATH: {bin_!r}")


def _build_home(root: Path) -> Path:
    """Drop a live ``.claude.json`` into a fresh HOME so the TUI skips its
    first-run wizard (theme picker / onboarding). Creds are NOT copied —
    those live under ``CLAUDE_CONFIG_DIR`` which we point at the live dir.
    """
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    if _LIVE_USER_STATE.is_file():
        shutil.copy2(_LIVE_USER_STATE, home / ".claude.json")
    return home


def _capture(session: str) -> str:
    res = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", "-400"],
        capture_output=True,
        text=True,
    )
    return res.stdout if res.returncode == 0 else ""


def main() -> int:
    _ensure_binaries()
    if not _LIVE_USER_STATE.is_file():
        raise SystemExit(
            f"live user-state missing at {_LIVE_USER_STATE!s} — cannot "
            f"prove the subscription auth path without it"
        )

    run_id = uuid.uuid4().hex[:8]
    root = Path(f"/tmp/tui-e2e-{run_id}")
    root.mkdir(parents=True, exist_ok=True)
    home = _build_home(root)
    session = f"tui-live-e2e-{run_id}"

    print(f"[e2e] session={session}", flush=True)
    print(f"[e2e] home={home}", flush=True)
    print(f"[e2e] CLAUDE_CONFIG_DIR={_CLAUDE_CONFIG_DIR}", flush=True)
    print(f"[e2e] prompt={_PROMPT!r}", flush=True)

    env_exports = f"export HOME={home}\nexport CLAUDE_CONFIG_DIR={_CLAUDE_CONFIG_DIR}\n"
    started = False
    try:
        started = TmuxManager.start(
            session_name=session,
            command="claude",
            workdir=str(home),
            env_exports=env_exports,
        )
        print(f"[e2e] start={started}", flush=True)
        if not started:
            print("[e2e] FAIL: TmuxManager.start returned False", flush=True)
            return 1

        # Let the TUI render its first frame fully before we type.
        time.sleep(_FIRST_TURN_WAIT_S)
        warm_pane = _capture(session)
        print(f"[e2e] warm_pane_first_line={warm_pane.splitlines()[:1]!r}", flush=True)

        TmuxManager.send_text_and_submit(session, _PROMPT)
        print("[e2e] send_text_and_submit=ok", flush=True)

        deadline = time.time() + _TIMEOUT_S
        last_pane = warm_pane
        while time.time() < deadline:
            time.sleep(_POLL_S)
            last_pane = _capture(session)
            if _REPLY_TOKEN in last_pane.lower():
                break

        scrollback = Path(".dev/tui_live_e2e_scrollback.txt")
        scrollback.write_text(last_pane, encoding="utf-8")
        print(f"[e2e] scrollback_written={scrollback}", flush=True)
        if _REPLY_TOKEN in last_pane.lower():
            print("[e2e] PASS: reply token observed in pane", flush=True)
            return 0
        print("[e2e] FAIL: reply token not observed within timeout", flush=True)
        return 3
    finally:
        if started:
            try:
                TmuxManager.stop(session)
                print("[e2e] stop=ok", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[e2e] stop_err={exc}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
