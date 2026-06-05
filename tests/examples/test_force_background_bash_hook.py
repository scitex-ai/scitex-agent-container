"""Pre-tool-use hook: structurally enforce background launch of unbounded Bash.

The operator's #1 fleet UX pain (lead directive 2026-06-04, operator
messages 8843 / 8845 / 8847 / 8852 / 8861 / 8862 / 8864): a foreground
Bash that runs for minutes blocks the conversation runner and delays
operator Telegram. Enforcement is a pre-tool-use hook — structural,
not behavioural — so an agent CANNOT accidentally run an unbounded
foreground Bash and queue the operator behind it.

Canonical policy (mirrors the lead's _base/to_home copy, dotfiles
commit ac582483):

  Allow foreground ONLY if BOUNDED:
    * ``run_in_background: true``                ← primary
    * explicit detach: trailing ``&``, ``nohup``, ``setsid``, ``disown``
    * ``timeout [1-7]s?`` wrapper
    * short trivial (<=50 chars, no pipe/redirect/chain, no
      long-runner first token)
  Else BLOCK with a WHY message that explains the Telegram-latency
  cause and offers four relaunch routes (Bash bg / setsid nohup / Task
  subagent / timeout 7).

  Escape: ``CC_ALLOW_FOREGROUND_HEAVY=1``.

The hook ships in the example agent template so future agents inherit
it; the lead's identical-policy copy in dotfiles ``_base/to_home`` is
the fleet rollout surface.

Each test asserts a single observable invariant. AAA layout. No mocks.
The hook script is invoked as a real subprocess on real stdin payloads.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

HOOK = (
    REPO_ROOT
    / "examples"
    / "agents"
    / "full-agent"
    / "to_home"
    / ".claude"
    / "hooks"
    / "pre-tool-use"
    / "force_background_bash.sh"
)


def _bash_payload(
    command: str,
    *,
    run_in_background: bool | None = None,
    timeout: int | None = None,
) -> str:
    body: dict = {"tool_name": "Bash", "tool_input": {"command": command}}
    if run_in_background is not None:
        body["tool_input"]["run_in_background"] = run_in_background
    if timeout is not None:
        body["tool_input"]["timeout"] = timeout
    return json.dumps(body)


def _run_hook(payload: str, *, env_override: dict[str, str] | None = None):
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
    )


# --- file / executable / self-test -----------------------------------------


def test_hook_file_exists() -> None:
    # Arrange
    target = HOOK
    # Act
    present = target.is_file()
    # Assert
    assert present, f"expected hook at {target}"


def test_hook_is_executable() -> None:
    # Arrange
    mode = HOOK.stat().st_mode
    # Act
    executable = bool(mode & stat.S_IXUSR)
    # Assert
    assert executable, "hook must have user-executable bit set"


def test_hook_self_test_passes() -> None:
    # Arrange
    cmd = ["bash", str(HOOK), "--self-test"]
    # Act
    result = subprocess.run(cmd, capture_output=True, text=True)
    # Assert
    assert result.returncode == 0, (
        f"self-test failed (rc={result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


# --- BLOCK cases mirrored from the lead's verbatim self-test --------------


def test_blocks_unbounded_pytest_command() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/ -x")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_unbounded_pdflatex_command() -> None:
    # Arrange
    payload = _bash_payload("pdflatex paper.tex")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_unbounded_make_command() -> None:
    # Arrange
    payload = _bash_payload("make -C builddir")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_unbounded_tectonic_command() -> None:
    # Arrange
    payload = _bash_payload("tectonic manuscript.tex")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_pipe_chain_command() -> None:
    # Arrange — `find ... | head -20` has a pipe, so not "short trivial".
    payload = _bash_payload("find /work -name '*.py' | head -20")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_install_then_pytest_chain() -> None:
    # Arrange — && chain; not trivial; contains heavy pytest tail.
    cmd = "uv pip install -e .[all] && python -m pytest -q"
    payload = _bash_payload(cmd)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_timeout_greater_than_seven_seconds() -> None:
    # Arrange — timeout 60 is well above the 1-7s bounded window.
    payload = _bash_payload("timeout 60 pytest tests/")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_timeout_with_minutes_unit() -> None:
    # Arrange — 7m is minutes; policy accepts 1-7 seconds (s or none).
    payload = _bash_payload("timeout 7m pytest tests/")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_long_sleep_command() -> None:
    # Arrange — sleep is in LONG_RE; not trivial; no bound.
    payload = _bash_payload("sleep 30")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_long_unbounded_install_command() -> None:
    # Arrange — npm install is LONG_RE; > 50 chars so not trivial-allowed.
    cmd = "npm install --no-audit --legacy-peer-deps"
    payload = _bash_payload(cmd)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


# --- ALLOW cases mirrored from the lead's verbatim self-test --------------


def test_allows_timeout_7_bounded_pytest() -> None:
    # Arrange
    payload = _bash_payload("timeout 7 pytest tests/ -x")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_timeout_7s_bounded_pdflatex() -> None:
    # Arrange
    payload = _bash_payload("timeout 7s pdflatex paper.tex")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_timeout_kill_flag_5s_make() -> None:
    # Arrange — timeout -k 1 5 make ...; value 5 (within 1-7).
    payload = _bash_payload("timeout -k 1 5 make -C builddir")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_pytest_run_in_background_true() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/ -x", run_in_background=True)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_setsid_nohup_explicit_detach() -> None:
    # Arrange
    cmd = "setsid nohup pdflatex paper.tex >/tmp/x.log 2>&1 &"
    payload = _bash_payload(cmd)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_make_detached_with_trailing_ampersand() -> None:
    # Arrange
    cmd = "make all >/tmp/b.log 2>&1 &"
    payload = _bash_payload(cmd)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_short_trivial_pwd() -> None:
    # Arrange
    payload = _bash_payload("pwd")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_short_trivial_date() -> None:
    # Arrange
    payload = _bash_payload("date")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_short_git_status_check() -> None:
    # Arrange
    payload = _bash_payload("git -C /work status -s")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_short_trivial_ls() -> None:
    # Arrange
    payload = _bash_payload("ls -la")
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


# --- numeric tool_input.timeout (enforce_delegation-style, lead-asked) ---


def test_allows_heavy_command_with_tool_timeout_5000ms() -> None:
    # Arrange — pytest is heavy, but the Bash tool's own timeout caps it.
    payload = _bash_payload("pytest tests/", timeout=5000)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_allows_heavy_command_with_tool_timeout_exactly_7000ms() -> None:
    # Arrange
    payload = _bash_payload("pdflatex paper.tex", timeout=7000)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_blocks_heavy_command_with_tool_timeout_15000ms() -> None:
    # Arrange — timeout is set but too large.
    payload = _bash_payload("pytest tests/", timeout=15000)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


def test_blocks_heavy_command_with_tool_timeout_zero() -> None:
    # Arrange — timeout=0 must NOT be treated as bounded.
    payload = _bash_payload("pytest tests/", timeout=0)
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 2


# --- non-Bash tool + escape hatch -----------------------------------------


def test_allows_non_bash_tool_read() -> None:
    # Arrange — Read tool with a file path; hook should not block.
    payload = json.dumps(
        {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}
    )
    # Act
    result = _run_hook(payload)
    # Assert
    assert result.returncode == 0


def test_honors_escape_hatch_env_var() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/")
    # Act
    result = _run_hook(payload, env_override={"CC_ALLOW_FOREGROUND_HEAVY": "1"})
    # Assert
    assert result.returncode == 0


# --- block message must explain the WHY (operator 8852 "説明も入れて") ----


def test_block_message_names_telegram_latency_cost() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/")
    # Act
    stderr = _run_hook(payload).stderr.lower()
    # Assert
    assert "telegram" in stderr


def test_block_message_explains_inbox_mechanism() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/")
    # Act
    stderr = _run_hook(payload).stderr.lower()
    # Assert — at least one of the canonical phrasings.
    assert any(marker in stderr for marker in ("inbox", "main loop", "main turn"))


def test_block_message_states_work_is_not_interrupted() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/")
    # Act
    stderr = _run_hook(payload).stderr.lower()
    # Assert — reassures the agent that work CONTINUES off the main loop.
    assert any(
        marker in stderr
        for marker in ("not interrupt", "continues", "off the main loop")
    )


def test_block_message_lists_all_four_relaunch_options() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/")
    # Act
    stderr = _run_hook(payload).stderr
    # Assert — all four canonical relaunch routes must be present.
    markers = (
        "run_in_background" in stderr
        and "setsid nohup" in stderr
        and ("Task" in stderr or "Agent" in stderr)
        and "timeout 7" in stderr
    )
    assert markers


def test_block_message_documents_escape_hatch_env() -> None:
    # Arrange
    payload = _bash_payload("pytest tests/")
    # Act
    stderr = _run_hook(payload).stderr
    # Assert
    assert "CC_ALLOW_FOREGROUND_HEAVY" in stderr


def test_block_message_quotes_operator_wording() -> None:
    # Arrange — operator's exact wording reframes the rule for any agent.
    payload = _bash_payload("pytest tests/")
    # Act
    stderr = _run_hook(payload).stderr
    # Assert
    assert "作業中断はしてほしくない" in stderr
