"""TUI runtime delivers Claude Code settings to the file the TUI reads.

Regression for the paper-scitex-clew solver run (2026-06-20): a
``runtime: tui`` solver hit live "Do you want to proceed?" permission
prompts AND reached DONE with an EMPTY clew DAG because:

  * ``setup_settings_json`` (skip-permissions + SAC channel hooks) was
    NEVER called on the TUI path — only the SDK / ``ClaudeCodeRuntime``
    path called it; AND
  * the ``_shared`` baseline honest-grounding Stop gate (``clew_verify_gate``)
    + lint PostToolUse (``run_lint``) were deployed to
    ``$HOME/.claude/settings.local.json`` — a path the interactive ``claude``
    TUI never reads at user scope. Claude discovers user-scope settings ONLY
    from ``$HOME/.claude/settings.json``; there is NO
    ``$HOME/.claude/settings.local.json`` (``.local.json`` is PROJECT-scope
    only, discovered at ``<cwd>/.claude/``). The ``--settings`` flag does not
    rescue this for the interactive TUI — it replaces discovery and applies
    only to print/SDK mode.

The fix (this branch):
  1. ``TuiSessionRuntime.materialize_workspace`` calls
     ``setup_settings_json(..., filename="settings.json")`` AFTER
     ``deploy_to_home`` so the managed keys deep-merge onto the baseline gate
     into ``$HOME/.claude/settings.json`` — the USER-scope file the TUI reads.
     It also folds any legacy ``$HOME/.claude/settings.local.json`` (from a
     baseline not yet renamed) forward into ``settings.json`` and removes it,
     so no critical hook is stranded at a path the TUI ignores.
  2. ``build_run_argv(tui=True)`` still emits ``--settings <container_home>/
     .claude/settings.json`` (belt-and-suspenders / SDK parity; the flag is a
     no-op for the interactive TUI but harmless).

These tests assert the END STATE the TUI actually reads: the settings file at
USER scope (``$HOME/.claude/settings.json``) carries ALL of skip-permissions +
the Stop gate + the PostToolUse lint + the SAC channel hooks; no critical hook
is left only in ``$HOME/.claude/settings.local.json``; and the launch argv
points the TUI at ``settings.json``.

Real ``AgentConfig`` via ``load_config`` on a tmp spec; baseline routed via
``SAC_TO_HOME_BASELINE``. No mocks (PA-306). STX-TQ002 AAA + STX-TQ007.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes import tui_session as _tui
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

# The ``_shared`` baseline ``settings.local.json`` the consuming project ships
# (scripts/harness/to_home/.claude/settings.local.json): the honest-grounding
# Stop gate + the scitex-io lint PostToolUse. Reproduced here as the baseline
# the TUI must preserve through the managed-key deep-merge.
_BASELINE_SETTINGS = {
    "hooks": {
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": 'bash "$HOME/.claude/hooks/stop/clew_verify_gate.sh"',
                    }
                ]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'bash "$HOME/.claude/hooks/post-tool-use/run_lint.sh"',
                    }
                ],
            }
        ],
    }
}

_SPEC_BODY = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  host: local
  workdir: /tmp/agt-work
  apptainer:
    image: /x.sif
    binds: []
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
  claude:
    model: claude-opus-4-8[1m]
    flags:
      - --dangerously-skip-permissions
"""


def _write_spec(tmp_path: Path) -> Path:
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(_SPEC_BODY, encoding="utf-8")
    return spec


def _write_baseline(tmp_path: Path, *, name: str = "settings.local.json") -> Path:
    """Materialise the ``_shared`` baseline settings file (gate+lint).

    ``name`` defaults to the LEGACY ``settings.local.json`` on purpose: it
    simulates a host whose baseline source has NOT been renamed yet, exercising
    the fold-forward in ``setup_settings_json`` (legacy sibling -> user-scope
    ``settings.json``). Pass ``name="settings.json"`` to simulate an already-
    renamed baseline.
    """
    baseline = tmp_path / "_shared_baseline" / "to_home"
    (baseline / ".claude").mkdir(parents=True, exist_ok=True)
    (baseline / ".claude" / name).write_text(
        json.dumps(_BASELINE_SETTINGS, indent=2) + "\n", encoding="utf-8"
    )
    return baseline


def _materialize(tmp_path: Path, *, baseline_name: str = "settings.local.json") -> Path:
    """Run ``materialize_workspace`` and return the container ``$HOME/.claude``.

    ``SAC_TO_HOME_BASELINE`` routes the shared baseline (gate+lint) into the
    deploy; ``state_dir_for_config`` is redirected (direct attribute swap, not
    the banned ``monkeypatch`` fixture — mirrors test_tui_session.py) so the
    workspace lands under ``tmp_path``. ``baseline_name`` chooses whether the
    baseline ships under the legacy ``settings.local.json`` (default) or the
    renamed ``settings.json``.
    """
    spec = _write_spec(tmp_path)
    baseline = _write_baseline(tmp_path, name=baseline_name)
    config = load_config(str(spec))

    state_dir = tmp_path / "state"
    saved_state_fn = _tui.state_dir_for_config
    saved_baseline = os.environ.get("SAC_TO_HOME_BASELINE")
    saved_home = os.environ.get("HOME")
    os.environ["SAC_TO_HOME_BASELINE"] = str(baseline)
    # Keep ensure_global_settings_json + any Path.home() use inside tmp.
    os.environ["HOME"] = str(tmp_path / "fake_home")
    _tui.state_dir_for_config = lambda _cfg: state_dir  # type: ignore[assignment]
    try:
        TuiSessionRuntime().materialize_workspace(config)
    finally:
        _tui.state_dir_for_config = saved_state_fn  # type: ignore[assignment]
        if saved_baseline is None:
            os.environ.pop("SAC_TO_HOME_BASELINE", None)
        else:
            os.environ["SAC_TO_HOME_BASELINE"] = saved_baseline
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
    return state_dir / "home" / ".claude"


@pytest.fixture
def materialized_settings(tmp_path: Path) -> dict:
    """Parsed ``$HOME/.claude/settings.json`` — the file the TUI reads.

    The baseline is seeded under the LEGACY ``settings.local.json`` to also
    exercise the fold-forward path; the END STATE the TUI discovers at user
    scope is ``settings.json``.
    """
    claude_dir = _materialize(tmp_path)
    settings_file = claude_dir / "settings.json"
    return json.loads(settings_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# materialize_workspace — the file the TUI reads carries ALL four pieces
# ---------------------------------------------------------------------------


def test_tui_settings_has_skip_permissions(materialized_settings: dict) -> None:
    # Arrange — materialized_settings = $HOME/.claude/settings.json (user scope).
    settings = materialized_settings
    # Act
    skip = settings.get("skipDangerousModePermissionPrompt")
    # Assert — skip-permissions key present (no live "proceed?" prompt).
    assert skip is True


def test_tui_settings_preserves_stop_clew_verify_gate(
    materialized_settings: dict,
) -> None:
    # Arrange
    stop_groups = materialized_settings["hooks"]["Stop"]
    # Act — collect every command string under the Stop event.
    commands = [
        h.get("command", "") for grp in stop_groups for h in grp.get("hooks", [])
    ]
    # Assert — the honest-grounding Stop gate survived the deep-merge.
    assert any("clew_verify_gate" in c for c in commands)


def test_tui_settings_preserves_posttooluse_run_lint(
    materialized_settings: dict,
) -> None:
    # Arrange
    post_groups = materialized_settings["hooks"]["PostToolUse"]
    # Act
    commands = [
        h.get("command", "") for grp in post_groups for h in grp.get("hooks", [])
    ]
    # Assert — the scitex-io lint PostToolUse survived the deep-merge.
    assert any("run_lint" in c for c in commands)


def test_tui_settings_has_sac_channel_hooks(materialized_settings: dict) -> None:
    # Arrange — SAC's event-ring hooks (event ingest) must coexist with
    # the baseline gate, across every event SAC wires.
    hooks = materialized_settings["hooks"]
    # Act — flatten every command in the merged hooks block.
    all_commands = [
        h.get("command", "")
        for groups in hooks.values()
        for grp in groups
        for h in grp.get("hooks", [])
    ]
    # Assert — at least one SAC event-ingest command is present.
    assert any("event ingest" in c for c in all_commands)


def test_tui_settings_stop_event_has_both_gate_and_sac_hook(
    materialized_settings: dict,
) -> None:
    # Arrange — the most regression-prone event: a naive existing.update()
    # would clobber the baseline Stop gate with SAC's Stop hook. The
    # deep-merge must keep BOTH under Stop.
    stop_commands = [
        h.get("command", "")
        for grp in materialized_settings["hooks"]["Stop"]
        for h in grp.get("hooks", [])
    ]
    # Act
    has_gate = any("clew_verify_gate" in c for c in stop_commands)
    has_sac = any("event ingest" in c for c in stop_commands)
    # Assert
    assert has_gate and has_sac


# ---------------------------------------------------------------------------
# USER-scope delivery: settings.json exists; nothing critical left in
# settings.local.json (the path the interactive TUI never reads at $HOME)
# ---------------------------------------------------------------------------


def _flatten_commands(data: dict) -> list[str]:
    """Every hook ``command`` string across every event in a settings dict."""
    return [
        h.get("command", "")
        for groups in data.get("hooks", {}).values()
        for grp in groups
        for h in grp.get("hooks", [])
    ]


def test_tui_user_scope_settings_json_exists(tmp_path: Path) -> None:
    # Arrange — materialise with the baseline under the legacy name.
    claude_dir = _materialize(tmp_path)
    # Act
    user_scope = claude_dir / "settings.json"
    # Assert — the file the interactive TUI reads at USER scope exists.
    assert user_scope.is_file()


def test_tui_user_scope_settings_json_keeps_baseline_gate(tmp_path: Path) -> None:
    # Arrange
    claude_dir = _materialize(tmp_path)
    data = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    # Act — flatten every command across every event in settings.json.
    all_cmds = _flatten_commands(data)
    # Assert — the baseline honest-grounding gate landed in the USER-scope file.
    assert any("clew_verify_gate" in c for c in all_cmds)


def test_tui_user_scope_settings_json_keeps_sac_hook(tmp_path: Path) -> None:
    # Arrange
    claude_dir = _materialize(tmp_path)
    data = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    # Act
    all_cmds = _flatten_commands(data)
    # Assert — a SAC channel hook landed in the USER-scope file too.
    assert any("event ingest" in c for c in all_cmds)


def test_tui_no_critical_hook_left_in_settings_local_json(tmp_path: Path) -> None:
    # Arrange — baseline seeded under the LEGACY settings.local.json; the
    # fold-forward must migrate it into settings.json and remove the sibling so
    # the honest-grounding gate is NOT stranded at a path the TUI never reads
    # at $HOME user scope.
    claude_dir = _materialize(tmp_path, baseline_name="settings.local.json")
    # Act
    legacy = claude_dir / "settings.local.json"
    # Assert — the legacy sibling is gone (its hooks were folded into
    # settings.json, asserted by the keeps_baseline_gate test above).
    assert not legacy.exists()


def test_tui_renamed_baseline_keeps_gate(tmp_path: Path) -> None:
    # Arrange — baseline already shipped as settings.json (post-rename host).
    claude_dir = _materialize(tmp_path, baseline_name="settings.json")
    data = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    # Act
    stop_cmds = [
        h.get("command", "")
        for grp in data["hooks"]["Stop"]
        for h in grp.get("hooks", [])
    ]
    # Assert — gate + SAC hook coexist under Stop (single boolean contract).
    assert any("clew_verify_gate" in c for c in stop_cmds) and any(
        "event ingest" in c for c in stop_cmds
    )


def test_tui_renamed_baseline_leaves_no_local_sibling(tmp_path: Path) -> None:
    # Arrange — baseline already shipped as settings.json (post-rename host).
    claude_dir = _materialize(tmp_path, baseline_name="settings.json")
    # Act
    legacy = claude_dir / "settings.local.json"
    # Assert — no spurious settings.local.json is created at user scope.
    assert not legacy.exists()


# ---------------------------------------------------------------------------
# build_run_argv(tui=True) — points the TUI at the settings file via --settings
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``HOME`` so the listen-bearer resolver stays inside the test."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path / "argv_home")
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def tui_config(tmp_path: Path):
    return load_config(str(_write_spec(tmp_path)))


def test_build_run_argv_tui_adds_settings_when_home_has_settings(
    tui_config, tmp_path, _isolate_home
) -> None:
    # Arrange — materialise a $HOME/.claude/settings.json under state.
    state_dir = tmp_path / "state"
    claude_dir = state_dir / "home" / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{}", encoding="utf-8")
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — the TUI is pointed at the in-container USER-scope settings file.
    assert "--settings /home/agent/.claude/settings.json" in " ".join(argv)


def test_build_run_argv_tui_settings_value_immediately_follows_flag(
    tui_config, tmp_path, _isolate_home
) -> None:
    # Arrange — under hardened mode the inner ``claude`` argv is wrapped in a
    # ``bash -c "<preflight>\nexec claude ..."`` string (the last argv element),
    # so we assert against that joined inner command rather than a top-level
    # argv element (matches how the --mcp-config tests assert).
    state_dir = tmp_path / "state"
    claude_dir = state_dir / "home" / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{}", encoding="utf-8")
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    inner = argv[-1]
    # Assert — the value sits immediately after its own --settings flag in the
    # claude invocation (not a joined ``--settings <path> <extra>`` pair).
    assert "--settings /home/agent/.claude/settings.json " in inner + " "


def test_build_run_argv_tui_accepts_legacy_settings_local_json(
    tui_config, tmp_path, _isolate_home
) -> None:
    # Arrange — a host whose baseline still ships only settings.local.json at
    # $HOME (not yet renamed): build_run_argv must still find a settings file to
    # point --settings at (fallback), so the SDK/print path keeps working.
    state_dir = tmp_path / "state"
    claude_dir = state_dir / "home" / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.local.json").write_text("{}", encoding="utf-8")
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — falls back to the legacy filename when that is all that exists.
    assert "--settings /home/agent/.claude/settings.local.json" in " ".join(argv)


def test_build_run_argv_tui_prefers_settings_json_over_local(
    tui_config, tmp_path, _isolate_home
) -> None:
    # Arrange — both files present; settings.json (user scope) must win.
    state_dir = tmp_path / "state"
    claude_dir = state_dir / "home" / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{}", encoding="utf-8")
    (claude_dir / "settings.local.json").write_text("{}", encoding="utf-8")
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — the user-scope settings.json is the one threaded to --settings.
    assert "--settings /home/agent/.claude/settings.json " in " ".join(argv) + " "


def test_build_run_argv_tui_omits_settings_when_no_settings_file(
    tui_config, tmp_path, _isolate_home
) -> None:
    # Arrange — state dir with a home but NO settings file at all.
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True)
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — no --settings flag aimed at a missing file.
    assert "--settings" not in argv
