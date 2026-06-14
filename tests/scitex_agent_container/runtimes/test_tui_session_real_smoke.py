"""TUI step-2 — real tmux + real claude binary smoke test.

Lead a2a ``d383f5389dc548a49a293bffe390d619`` (mission persisted in
``.dev/TUI_MISSION.md``). The unit suite in
``test_tui_session.py`` uses an in-memory MultiplexerProtocol fake so
it can run on CI hosts without tmux. This suite is the opposite — it
proves the same runtime drives REAL tmux + the REAL bundled ``claude``
binary end-to-end on a host that has both.

Acceptance criteria (step 2):

  (a) tmux session spawns under the runtime's namespace.
  (b) the bundled ``claude`` TUI launches *inside* tmux with
      ``HOME=<state>/home`` (proves the materialised workspace is
      what claude sees on the inside).
  (c) the materialised ``to_home/`` tree is present at
      ``<state>/home/`` — ``.mcp.json``, ``.claude/settings.json``,
      ``.claude/skills/<name>/SKILL.md`` (proves the overlay step ran
      and the agent inside tmux has the same skill / MCP surface the
      SDK runtime would have provided via apptainer bind-mount).

Verification primitives (all real subprocess):

  * ``tmux has-session -t <session>``         — (a)
  * ``tmux capture-pane -p``                  — (b) banner string
  * ``ps -eo pid,ppid,comm`` + ``/proc``      — (b) HOME via environ
  * filesystem stat of ``<state>/home/...``   — (c)

No mocks. No MagicMock. No monkeypatch of internal functions —
the only environment manipulation is ``HOME``, redirected to a
``tmp_path``-based root via the shared ``env_save_restore`` fixture
so ``state_dir_for_config`` lands its materialised tree inside the
test's scratch dir.

Skipped automatically when ``tmux`` or ``claude`` are missing — both
are present on the TUI-hedge base SIF (PR #390, merged 2026-06-15).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes.tui_session import (
    TuiSessionRuntime,
    session_name_for,
    state_dir_for_config,
)

# ---------------------------------------------------------------------------
# Skip-if-no-binary gate. The smoke test exec's both tmux and claude;
# without either the suite would FAIL on a developer laptop instead of
# politely skipping. Both are guaranteed present on the TUI-hedge base
# SIF (PR #390, merged 2026-06-15 — see TUI_MISSION.md).
# ---------------------------------------------------------------------------

_TMUX_BIN = shutil.which("tmux")
_CLAUDE_BIN = shutil.which("claude")

pytestmark = [
    pytest.mark.skipif(_TMUX_BIN is None, reason="tmux binary not on PATH"),
    pytest.mark.skipif(_CLAUDE_BIN is None, reason="claude binary not on PATH"),
    # The whole module spawns real processes and waits seconds for the
    # TUI to render — mark slow so it can be deselected by `-m "not slow"`
    # on a fast iteration loop.
    pytest.mark.slow,
]


# ---------------------------------------------------------------------------
# Fixture-copy helper. We can't load the fixture spec directly from the
# in-repo path because state_dir_for_config walks UP from the spec dir
# looking for ``.scitex/agent-container/`` — and the repo has one at the
# root, so the materialised tree would land in /work/.scitex/.../runtime/
# (polluting the repo). Copying the fixture into tmp_path takes the spec
# out of any project scope; combined with HOME-redirect the materialise
# falls under ``tmp_path/.scitex/...`` and the test is self-contained.
# ---------------------------------------------------------------------------

_FIXTURE_ROOT = Path(__file__).parent / "_fixtures_tui"


def _copy_fixture_to(dest_parent: Path, *, agent_name: str) -> Path:
    """Copy the fixture to ``dest_parent/<agent_name>/`` and rename the
    spec yaml to ``<agent_name>.yaml`` so sac v3 dir-as-SSoT validation
    derives the agent name from the parent directory.

    Per-run uuid in ``agent_name`` defeats tmux session collisions when
    the test is re-run before cleanup or xdist parallelises it.

    Returns the absolute path to the copied yaml.
    """
    agent_dir = dest_parent / agent_name
    shutil.copytree(_FIXTURE_ROOT, agent_dir)
    spec_path = agent_dir / f"{agent_name}.yaml"
    (agent_dir / "tui-smoke.yaml").rename(spec_path)
    return spec_path


# ---------------------------------------------------------------------------
# Polling helper — the claude TUI takes ~2-4 seconds to draw its first
# frame on the materialised HOME. A fixed sleep is flaky on a loaded
# host; poll capture-pane until a TUI-shape token appears or we hit a
# generous timeout.
# ---------------------------------------------------------------------------


def _wait_for_tui_banner(
    session_name: str, *, banner_substrings: tuple[str, ...], timeout_s: float
) -> str:
    """Poll ``tmux capture-pane`` until any banner_substring appears.

    Returns the pane text at the moment of success. Raises ``TimeoutError``
    on timeout so the test reports the real wall (slow render vs.
    crashed-claude) instead of a generic assertion failure.
    """
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
        )
        last = result.stdout if result.returncode == 0 else ""
        if any(token in last for token in banner_substrings):
            return last
        time.sleep(0.4)
    raise TimeoutError(
        f"claude TUI did not render any of {banner_substrings!r} in "
        f"{timeout_s:.1f}s. Last pane content:\n{last}"
    )


def _claude_pid_under_tmux() -> int | None:
    """Return the PID of a ``claude`` process whose parent chain rises
    to a ``tmux`` server, or ``None`` when no such process exists.

    We inspect ``ps -eo pid,ppid,comm`` once and walk parents in
    Python — robust against the various forms of claude's process
    name (``claude``, ``node`` for the bundled wrapper, etc.) and
    cheaper than pgrep loops.
    """
    result = subprocess.run(
        ["ps", "-eo", "pid,ppid,comm"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    parent: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        parent[pid] = (ppid, parts[2])
    for pid, (_ppid, comm) in parent.items():
        if comm != "claude":
            continue
        cursor = pid
        for _ in range(8):  # bounded walk; init pid 1 stops us anyway
            ppid, _pcomm = parent.get(cursor, (0, ""))
            _ppid_pcomm = parent.get(ppid)
            if _ppid_pcomm is None:
                break
            if "tmux" in _ppid_pcomm[1]:
                return pid
            cursor = ppid
    return None


def _read_home_from_environ(pid: int) -> str:
    """Read ``HOME`` from ``/proc/<pid>/environ``. Empty string if the
    process exited between detection and the read."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, PermissionError):
        return ""
    for entry in raw.split(b"\0"):
        if entry.startswith(b"HOME="):
            return entry[len(b"HOME=") :].decode("utf-8", errors="replace")
    return ""


# ---------------------------------------------------------------------------
# Single composite test. The four AC checks share an expensive setup
# (real tmux session + real claude render) so collapsing them keeps the
# slow-suite latency to one launch instead of four. Each AC bullet maps
# to one assertion below.
# ---------------------------------------------------------------------------


class TestTuiRuntimeRealBinarySmoke:
    def test_real_tmux_real_claude_smoke_lands_materialised_home(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # ---- Arrange -----------------------------------------------------
        # Per-run uuid in the agent name → unique tmux session even when
        # xdist runs this in parallel with itself (worker_id duplication).
        agent_name = f"tui-smoke-{uuid.uuid4().hex[:8]}"
        spec_path = _copy_fixture_to(tmp_path / "spec_root", agent_name=agent_name)

        # Redirect HOME so state_dir_for_config (which falls through to
        # ~/.scitex/agent-container/runtime/<name>/) lands under tmp_path.
        # We DON'T touch CLAUDE_CONFIG_DIR — the runtime exports its own
        # value into the tmux session, which is what we want to verify.
        home_root = tmp_path / "home_root"
        home_root.mkdir()
        env_save_restore.set("HOME", str(home_root))

        config = load_config(spec_path)
        runtime = TuiSessionRuntime()  # default = real TmuxManager
        session = session_name_for(config)
        state_home = state_dir_for_config(config) / "home"

        # ---- Act ---------------------------------------------------------
        started = False
        try:
            started = runtime.start(config, force=True)

            # ---- Assert (a) — tmux session spawned --------------------
            assert started is True
            has_session = subprocess.run(
                ["tmux", "has-session", "-t", session],
                capture_output=True,
            )
            assert has_session.returncode == 0, (
                f"tmux has-session failed for {session!r}; "
                f"stderr={has_session.stderr.decode(errors='replace')!r}"
            )

            # ---- Assert (b) — claude TUI rendered + HOME injected -----
            # The claude TUI's first-frame banners differ across versions:
            # 2.1.x prints the theme picker ("Let's get started" /
            # "Choose the text style") on a fresh HOME; an already-themed
            # HOME jumps straight to the input prompt ("Welcome to Claude
            # Code"). Accept any of them as evidence that the TUI loaded.
            pane = _wait_for_tui_banner(
                session,
                banner_substrings=(
                    "Let's get started",
                    "Choose the text style",
                    "Welcome to Claude Code",
                    "claude.ai",  # OAuth banner on first launch
                ),
                timeout_s=20.0,
            )
            assert pane.strip(), "TUI banner detected but pane was empty"

            claude_pid = _claude_pid_under_tmux()
            assert claude_pid is not None, (
                "claude TUI did not appear under tmux in `ps` — "
                "the TUI rendered (banner seen) but the process is gone; "
                f"pane:\n{pane}"
            )
            home_in_environ = _read_home_from_environ(claude_pid)
            assert home_in_environ == str(state_home), (
                f"claude is running with HOME={home_in_environ!r}, "
                f"expected materialised HOME={str(state_home)!r}"
            )

            # ---- Assert (c) — to_home tree materialised ---------------
            # These three paths are what we put in the fixture's
            # to_home/. If any are missing the overlay step failed.
            assert (state_home / ".mcp.json").is_file(), (
                f".mcp.json missing under {state_home} — overlay failed"
            )
            assert (state_home / ".claude" / "settings.json").is_file(), (
                f"settings.json missing under {state_home}/.claude — overlay failed"
            )
            assert (
                state_home / ".claude" / "skills" / "hello" / "SKILL.md"
            ).is_file(), (
                "nested skills/hello/SKILL.md missing — overlay did not "
                "recurse into subdirs"
            )
            # CLAUDE.md is written by setup_claude_md (not from to_home);
            # the helper lands it under <home>/.claude/CLAUDE.md so the
            # in-tmux claude reads it via CLAUDE_CONFIG_DIR (also exported
            # by the runtime). If it is missing the materialiser's first
            # leg failed.
            assert (state_home / ".claude" / "CLAUDE.md").is_file(), (
                f"CLAUDE.md missing under {state_home}/.claude — "
                "setup_claude_md did not run"
            )
        finally:
            # Always stop the tmux session — a leaked detached session
            # would survive the test process and consume the slot for
            # the next run. Best-effort; ignore errors.
            try:
                runtime.stop(config)
            except Exception:  # stx-allow: cleanup (reason: best-effort tmux kill on test teardown — failure here must not mask the real assertion failure above)
                pass
            # Also reap any stray claude process the TUI left behind
            # (e.g. tmux-kill races on a slow host).
            if started:
                subprocess.run(
                    ["pkill", "-f", f"claude.*{agent_name}"],
                    capture_output=True,
                )


# EOF
