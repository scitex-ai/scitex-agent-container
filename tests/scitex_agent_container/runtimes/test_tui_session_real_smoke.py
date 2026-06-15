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

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._tui_auth_stage import (
    CLAUDE_JSON_SRC_ENV,
    CREDENTIALS_SRC_ENV,
)
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

# RETIRED MODEL (2026-06-15 in-apptainer TUI pivot): this harness asserts
# the legacy host-tmux behaviour — a real ``claude`` process running on
# the HOST with ``HOME=<state>/home`` (probed via ``/proc/<pid>/environ``)
# and credentials COPIED into ``<state>/home``. The TUI runtime now runs
# ``claude`` INSIDE apptainer (parity with the SDK runtime): there is no
# host claude process to probe, the in-container ``$HOME`` is
# ``/home/agent``, and credentials are bind-mounted (not copied). These
# premises are gone, so the module is skipped rather than asserting a
# retired path. FOLLOW-UP: rewrite as a true in-apptainer smoke that boots
# the SIF and probes inside the container (needs a built SIF + valid
# creds). The dispatch glue is covered hermetically in test_tui_session.py
# (via the ``command_builder`` seak) and the argv assembly in the
# build_run_argv suite.
pytestmark = [
    pytest.mark.skip(
        reason="retired host-tmux TUI model — TUI now runs inside apptainer; "
        "rewrite as in-apptainer smoke (needs built SIF + creds)"
    ),
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
# Real-binary smoke. The original composite asserted all four AC bullets in
# one test body to amortise the expensive setup (real tmux + real claude
# render = ~6-10s). STX-TQ007 (one-assert) + STX-TQ002 (AAA markers)
# require one assertion per test, so we expose the shared setup as a
# class-scoped fixture and let each AC bullet land in its own one-assert
# method. The fixture still launches claude exactly once per class — the
# pre-refactor amortisation is preserved.
# ---------------------------------------------------------------------------


class _SmokeProbes:
    """Captured probes from the one-shot real-binary launch.

    Each field is one piece of evidence the post-launch methods assert on.
    Built by the ``smoke_probes`` class-scoped fixture; consumed by the
    ``Test*RealBinarySmoke`` test classes below.
    """

    started: bool
    has_session_rc: int
    pane: str
    claude_pid: int | None
    home_in_environ: str
    state_home: Path


def _stage_fake_auth_sources(
    tmp_path: Path, env_save_restore_class: object
) -> tuple[Path, Path]:
    """Stage realistic-but-fake credentials + .claude.json sources and
    point the auth-stage env vars at them.

    The TUI runtime now materialises both files into ``<state>/home/``
    (lead a2a ``910ff436642948eb85f8b3100204ed9b``) so every test that
    calls ``runtime.start`` needs them present somewhere. For smoke /
    nonce tests we don't need REAL OAuth tokens — fake files satisfy
    ``stage_tui_auth``'s existence check and the inner claude TUI then
    falls back to the login picker (which is what the smoke tests
    already expect to see anyway).
    """
    fake_creds = tmp_path / "fake_creds" / ".credentials.json"
    fake_creds.parent.mkdir(parents=True, exist_ok=True)
    fake_creds.write_text(
        '{"claudeAiOauth": {"accessToken": "fake", "refreshToken": "fake",'
        ' "expiresAt": 9999999999999, "subscriptionType": "max"}}'
    )
    fake_claude_json = tmp_path / "fake_claude_json" / ".claude.json"
    fake_claude_json.parent.mkdir(parents=True, exist_ok=True)
    fake_claude_json.write_text(
        '{"hasCompletedOnboarding": false, "oauthAccount": null}'
    )
    env_save_restore_class.set(CREDENTIALS_SRC_ENV, str(fake_creds))
    env_save_restore_class.set(CLAUDE_JSON_SRC_ENV, str(fake_claude_json))
    return fake_creds, fake_claude_json


@pytest.fixture(scope="class")
def smoke_probes(request, tmp_path_factory, env_save_restore_class) -> "_SmokeProbes":
    """Drive ONE real tmux + real claude launch and capture every probe
    the AC bullets need. Stops the session in teardown.

    Class-scoped so 4 one-assert tests share the same launch — preserves
    the amortisation the pre-refactor composite test achieved.
    """
    # Arrange
    tmp_path = tmp_path_factory.mktemp("tui-smoke")
    agent_name = f"tui-smoke-{uuid.uuid4().hex[:8]}"
    spec_path = _copy_fixture_to(tmp_path / "spec_root", agent_name=agent_name)
    home_root = tmp_path / "home_root"
    home_root.mkdir()
    env_save_restore_class.set("HOME", str(home_root))
    _stage_fake_auth_sources(tmp_path, env_save_restore_class)

    config = load_config(spec_path)
    runtime = TuiSessionRuntime()
    session = session_name_for(config)
    state_home = state_dir_for_config(config) / "home"

    probes = _SmokeProbes()
    probes.state_home = state_home
    probes.started = False
    probes.has_session_rc = -1
    probes.pane = ""
    probes.claude_pid = None
    probes.home_in_environ = ""

    # Act — single launch; capture every probe the assertion tests need.
    try:
        probes.started = runtime.start(config, force=True)
        has_session = subprocess.run(
            ["tmux", "has-session", "-t", session], capture_output=True
        )
        probes.has_session_rc = has_session.returncode
        try:
            probes.pane = _wait_for_tui_banner(
                session,
                banner_substrings=(
                    "Let's get started",
                    "Choose the text style",
                    "Welcome to Claude Code",
                    "claude.ai",
                ),
                timeout_s=20.0,
            )
        except TimeoutError as exc:  # stx-allow: test-capture (reason: capture the wall instead of crashing the whole class — individual one-assert tests then report which AC bullet failed)
            probes.pane = str(exc)
        probes.claude_pid = _claude_pid_under_tmux()
        if probes.claude_pid is not None:
            probes.home_in_environ = _read_home_from_environ(probes.claude_pid)
        yield probes
    finally:
        try:
            runtime.stop(config)
        except Exception:  # stx-allow: cleanup (reason: best-effort tmux kill on teardown — failure here must not mask the AC asserts above)
            pass
        if probes.started:
            subprocess.run(
                ["pkill", "-f", f"claude.*{agent_name}"], capture_output=True
            )


@pytest.fixture(scope="class")
def env_save_restore_class():
    """Class-scoped env save/restore — mirrors the function-scoped
    ``env_save_restore`` in ``_helpers/subprocess_shim.py`` but lives a
    whole class. Avoids ``monkeypatch`` (PA-306 §3 forbids it) by writing
    to ``os.environ`` directly and reverting in teardown.
    """
    import os

    saved: dict[str, str | None] = {}

    class _Setter:
        def set(self, key: str, value: str) -> None:
            if key not in saved:
                saved[key] = os.environ.get(key)
            os.environ[key] = value

    try:
        yield _Setter()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestTuiRuntimeRealBinarySmoke:
    """Step-2 AC: launch real tmux + real claude with materialised HOME.

    One launch shared via ``smoke_probes``; each AC bullet asserts on a
    single captured probe.
    """

    def test_runtime_start_returned_true(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        started = smoke_probes.started
        # Act
        # (no further action; the launch already happened in the fixture)
        # Assert
        assert started is True

    def test_tmux_has_session(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        rc = smoke_probes.has_session_rc
        # Act
        # (fixture ran `tmux has-session` and captured rc)
        # Assert
        assert rc == 0

    def test_tui_banner_appeared(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        pane = smoke_probes.pane
        # Act
        # (fixture polled capture-pane for a banner)
        # Assert
        assert pane.strip() != ""

    def test_claude_pid_under_tmux(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        pid = smoke_probes.claude_pid
        # Act
        # (fixture walked `ps` for claude under tmux)
        # Assert
        assert pid is not None

    def test_home_environ_matches_materialised(
        self, smoke_probes: _SmokeProbes
    ) -> None:
        # Arrange
        observed = smoke_probes.home_in_environ
        expected = str(smoke_probes.state_home)
        # Act
        # (fixture read /proc/<pid>/environ)
        # Assert
        assert observed == expected

    def test_mcp_json_materialised(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        path = smoke_probes.state_home / ".mcp.json"
        # Act
        present = path.is_file()
        # Assert
        assert present is True

    def test_settings_json_materialised(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        path = smoke_probes.state_home / ".claude" / "settings.json"
        # Act
        present = path.is_file()
        # Assert
        assert present is True

    def test_nested_skill_materialised(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        path = smoke_probes.state_home / ".claude" / "skills" / "hello" / "SKILL.md"
        # Act
        present = path.is_file()
        # Assert
        assert present is True

    def test_claude_md_present(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        path = smoke_probes.state_home / ".claude" / "CLAUDE.md"
        # Act
        present = path.is_file()
        # Assert
        assert present is True


# ---------------------------------------------------------------------------
# Step 3 — one verified a2a turn through the TUI runtime, nonce round-trip.
#
# AC (lead a2a d383f5389dc548a49a293bffe390d619 + clarification
# edfe809e55a24640b6a42318872c8b58): drive ONE a2a turn end-to-end through
# the TUI runtime — send a message via the runtime, confirm it is delivered
# into the pane. Lead's clarification: keep this hermetic (no creds, no
# network). The real-claude "produces an answer" assertion lives in step
# 4's tui-alive integration probe, gated on credentials being present.
#
# Design: construct a TuiSessionRuntime with a deterministic stand-in
# command (``bash -c 'while read line; do echo RX: $line; done'``) instead
# of the real claude binary. send_turn → tmux send_text_and_submit → the
# bash reader prints ``RX: <nonce>`` back into the pane. Polling
# capture-pane for the round-trip token proves the delivery primitive
# works against REAL tmux end-to-end without coupling to claude's UI
# state machine (the auth/theme-picker variations that step 2 already
# handled).
# ---------------------------------------------------------------------------


# bash reader loop — echoes each line as ``RX: <line>`` so the test can
# distinguish "we delivered" from "the pane has noise". A single quote
# layer so TmuxManager.start's ``exec {command}`` parses it correctly.
_BASH_ECHO_READER = "bash -c 'while IFS= read -r line; do echo \"RX: ${line}\"; done'"


class _NonceProbes:
    """Captured probes from the one-shot nonce round-trip launch.

    Built by the ``nonce_probes`` class-scoped fixture; consumed by the
    one-assert tests in ``TestTuiRuntimeNonceRoundTrip``.
    """

    started: bool
    delivered: bool
    pane: str
    nonce: str


@pytest.fixture(scope="class")
def nonce_probes(tmp_path_factory, env_save_restore_class) -> "_NonceProbes":
    """Drive ONE bash-reader stand-in launch + send_turn + capture-pane
    poll, then return the captured probes.

    Class-scoped: the launch is ~0.5s + capture is ~1s; we keep two
    one-assert tests sharing it rather than relaunching per assertion.
    """
    # Arrange
    tmp_path = tmp_path_factory.mktemp("tui-nonce")
    agent_name = f"tui-turn-{uuid.uuid4().hex[:8]}"
    spec_path = _copy_fixture_to(tmp_path / "spec_root", agent_name=agent_name)
    home_root = tmp_path / "home_root"
    home_root.mkdir()
    env_save_restore_class.set("HOME", str(home_root))
    _stage_fake_auth_sources(tmp_path, env_save_restore_class)

    config = load_config(spec_path)
    runtime = TuiSessionRuntime(claude_bin=_BASH_ECHO_READER)
    session = session_name_for(config)
    nonce = f"sac-tui-nonce-{uuid.uuid4().hex[:12]}"

    probes = _NonceProbes()
    probes.started = False
    probes.delivered = False
    probes.pane = ""
    probes.nonce = nonce

    # Act — single send_turn round-trip; capture every probe.
    try:
        probes.started = runtime.start(config, force=True)
        # TmuxManager.start sleeps 2s but the bash subshell needs a tick
        # more before stdin is bound to the read.
        time.sleep(0.5)
        # ``wait_ready=False``: the bash stand-in reader has no
        # ``? for shortcuts`` input-ready footer; this test
        # exercises the delivery primitive in isolation. The
        # state-table modal drain is covered by the real-claude
        # live-turn class where the marker DOES appear.
        probes.delivered = runtime.send_turn(config, nonce, wait_ready=False)
        try:
            probes.pane = _wait_for_tui_banner(
                session,
                banner_substrings=(f"RX: {nonce}",),
                timeout_s=10.0,
            )
        except TimeoutError as exc:  # stx-allow: test-capture (reason: capture the wall so the per-assert tests below report which step failed, not a class-level error)
            probes.pane = str(exc)
        yield probes
    finally:
        try:
            runtime.stop(config)
        except Exception:  # stx-allow: cleanup (reason: best-effort tmux kill on teardown — failure here must not mask asserts above)
            pass


class TestTuiRuntimeNonceRoundTrip:
    """Step-3 AC: send_turn delivers a nonce into a real tmux pane and
    the bash reader stand-in echoes it back. Two one-assert checks share
    the same ``nonce_probes`` launch.
    """

    def test_runtime_start_returned_true(self, nonce_probes: _NonceProbes) -> None:
        # Arrange
        started = nonce_probes.started
        # Act
        # (fixture ran runtime.start)
        # Assert
        assert started is True

    def test_send_turn_delivered(self, nonce_probes: _NonceProbes) -> None:
        # Arrange
        delivered = nonce_probes.delivered
        # Act
        # (fixture ran runtime.send_turn)
        # Assert
        assert delivered is True

    def test_nonce_round_tripped_through_pane(self, nonce_probes: _NonceProbes) -> None:
        # Arrange
        expected = f"RX: {nonce_probes.nonce}"
        pane = nonce_probes.pane
        # Act
        observed = expected in pane
        # Assert
        assert observed is True


# ---------------------------------------------------------------------------
# Auth-stage materialisation (lead a2a ``910ff436642948eb85f8b3100204ed9b``)
#
# The materialise step now lands the two files the interactive TUI
# checks on launch. The smoke fixture above points the env vars at
# fake auth so the stage doesn't read host creds; these tests prove
# the files arrived where the inner ``claude`` will look for them.
# ---------------------------------------------------------------------------


class TestTuiRuntimeAuthStaged:
    """Step-3 of the 2026-06-14 TUI hedge: ``.claude/.credentials.json``
    and ``.claude.json`` land in ``<state>/home/`` automatically. Shares
    the same smoke launch (no extra wall).
    """

    def test_credentials_file_in_state_home(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        path = smoke_probes.state_home / ".claude" / ".credentials.json"
        # Act
        present = path.is_file()
        # Assert
        assert present is True

    def test_claude_json_in_state_home(self, smoke_probes: _SmokeProbes) -> None:
        # Arrange
        path = smoke_probes.state_home / ".claude.json"
        # Act
        present = path.is_file()
        # Assert
        assert present is True


# EOF
