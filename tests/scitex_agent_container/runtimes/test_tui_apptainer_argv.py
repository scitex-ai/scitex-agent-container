"""In-apptainer TUI runtime — argv assembly + credential-file mount.

Covers the 2026-06-15 pivot where ``spec.runtime: tui`` runs the
interactive ``claude`` TUI INSIDE apptainer (parity with the SDK
runtime) instead of on the host via tmux:

  * ``build_inner_argv(config, tui=True)`` → interactive ``claude``
    (model + flags) as the inner process, not the ``python -m`` runner.
  * ``build_run_argv(config, tui=True)`` → a full ``apptainer exec ...``
    argv whose inner command is ``claude``.
  * ``credentials_file_bind`` → the designated ``spec.claude.
    credentials_file`` is bind-mounted WRITABLE at the container
    ``$HOME/.claude/.credentials.json`` (single source of truth), and
    emitted AFTER the sif path's preceding binds so a relaxed ``--home``
    tmpfs cannot shadow it.

Real AgentConfig via ``load_config`` on a tmp spec — no mocks.
STX-TQ002 AAA-marker + STX-TQ007 one-assert.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_auth import credentials_file_bind
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    build_inner_argv,
    tui_channel_config,
)


def _write_spec(tmp_path: Path, body: str) -> Path:
    spec_dir = tmp_path / "agents" / "agt"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = spec_dir / "spec.yaml"
    spec.write_text(body, encoding="utf-8")
    return spec


_BASE_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
spec:
  runtime: tui
  workdir: /tmp/agt-work
  claude:
    model: claude-opus-4-8[1m]
    flags:
      - --dangerously-skip-permissions
{extra}
"""


@pytest.fixture
def tui_config(tmp_path):
    spec = _write_spec(tmp_path, _BASE_SPEC.format(extra=""))
    return load_config(str(spec))


# ---------------------------------------------------------------------------
# build_inner_argv(tui=True) — interactive claude as the inner process
# ---------------------------------------------------------------------------


def test_tui_inner_argv_runs_claude_binary(tui_config) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    inner = build_inner_argv(tui_config, tui=True)
    # Assert — the inner command is the interactive claude TUI.
    assert inner[0] == "claude"


def test_tui_inner_argv_threads_model_flag(tui_config) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    inner = build_inner_argv(tui_config, tui=True)
    # Assert
    assert "--model" in inner and "claude-opus-4-8[1m]" in inner


def test_tui_inner_argv_appends_spec_flags(tui_config) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    inner = build_inner_argv(tui_config, tui=True)
    # Assert
    assert "--dangerously-skip-permissions" in inner


def test_tui_inner_argv_is_not_the_python_runner(tui_config) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    inner = build_inner_argv(tui_config, tui=True)
    # Assert — never dispatches the SDK session runner module.
    assert not any("claude_session" in part for part in inner)


# ---------------------------------------------------------------------------
# build_run_argv(tui=True) — full apptainer exec wrapping claude
# ---------------------------------------------------------------------------


def test_build_run_argv_tui_is_apptainer_exec(tui_config, tmp_path) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    argv = build_run_argv(
        tui_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert
    assert argv[:2] == ["apptainer", "exec"]


def test_build_run_argv_tui_inner_is_claude(tui_config, tmp_path) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    argv = build_run_argv(
        tui_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert — the inner command is the interactive claude TUI (wrapped
    # by the preflight ``exec`` on non-relaxed specs).
    assert "claude --model" in " ".join(argv)


# ---------------------------------------------------------------------------
# credentials_file_bind — writable single-source-of-truth mount
# ---------------------------------------------------------------------------


def test_credentials_file_bind_empty_when_unset(tui_config) -> None:
    # Arrange — config from the tui_config fixture (no credentials_file).
    # Act
    flags = credentials_file_bind(tui_config)
    # Assert
    assert flags == []


def test_credentials_file_bind_mounts_designated_file_rw(tmp_path) -> None:
    # Arrange — a designated credentials file on disk.
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}", encoding="utf-8")
    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(extra=f"    credentials_file: {creds}"),
    )
    config = load_config(str(spec))
    # Act
    flags = credentials_file_bind(config)
    # Assert — writable bind onto the canonical container creds path.
    assert flags == [
        "--bind",
        f"{creds}:/home/agent/.claude/.credentials.json:rw",
    ]


def test_credentials_file_bind_missing_file_fails_loud(tmp_path) -> None:
    # Arrange — designate a path that does not exist.
    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(extra=f"    credentials_file: {tmp_path / 'nope.json'}"),
    )
    config = load_config(str(spec))
    # Act
    raises = pytest.raises(FileNotFoundError)
    # Assert
    with raises:
        credentials_file_bind(config)


def test_build_run_argv_tui_adds_mcp_config_when_home_has_mcp_json(
    tui_config, tmp_path
) -> None:
    # Arrange — materialise a $HOME/.mcp.json under the state dir.
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True)
    (state_dir / "home" / ".mcp.json").write_text("{}", encoding="utf-8")
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — the TUI is pointed at the in-container .mcp.json.
    assert "--mcp-config /home/agent/.mcp.json" in " ".join(argv)


def test_build_run_argv_tui_omits_mcp_config_when_no_mcp_json(
    tui_config, tmp_path
) -> None:
    # Arrange — state dir with a home but NO .mcp.json.
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True)
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert
    assert "--mcp-config" not in argv


_SPEC_WITH_CHANNEL = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
spec:
  runtime: tui
  workdir: /tmp/agt-work
  claude:
    model: sonnet
    channels:
      - server:sac
"""


def test_tui_channel_config_none_when_no_channels(tui_config) -> None:
    # Arrange — tui_config has no channels.
    # Act
    dev_channels, channel_mcp = tui_channel_config(tui_config)
    # Assert
    assert (dev_channels, channel_mcp) == (None, None)


def test_tui_channel_config_sets_dev_channels(tmp_path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_WITH_CHANNEL)
    config = load_config(str(spec))
    # Act
    dev_channels, _ = tui_channel_config(config)
    # Assert
    assert dev_channels == "server:sac"


def test_tui_channel_config_registers_sac_channel_subscriber(tmp_path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_WITH_CHANNEL)
    config = load_config(str(spec))
    # Act
    _, channel_mcp = tui_channel_config(config)
    # Assert — inline MCP JSON registers the absolute-path sac channel sub.
    assert channel_mcp is not None and "mcp" in channel_mcp and "channel" in channel_mcp


def test_build_run_argv_tui_adds_dev_channels_flag(tmp_path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_WITH_CHANNEL)
    config = load_config(str(spec))
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True)
    # Act
    argv = build_run_argv(
        config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — flag rides in the inner cmd (preflight-wrapped on non-relaxed).
    assert "--dangerously-load-development-channels server:sac" in " ".join(argv)


def test_build_run_argv_tui_injects_channel_subscriber_mcp(tmp_path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_WITH_CHANNEL)
    config = load_config(str(spec))
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True)
    # Act
    argv = build_run_argv(
        config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — the inline sac-channel subscriber rides on --mcp-config
    # (preflight-wrapped on non-relaxed specs, so check the joined argv).
    # The command must be the in-SIF absolute path of the `sac` console
    # script. The default is /opt/venv-agent/bin/sac (verified in
    # sac-base.sif 2026-06-15; the old /opt/venv-sac/bin/sac hardcode
    # was wrong — the binary lives under the agent venv, not the SDK
    # venv). The bare command must NOT be just `sac` (PR #407 silent-
    # exec-fail class).
    joined = " ".join(argv)
    assert '"command": "/opt/venv-agent/bin/sac"' in joined and '"channel"' in joined


def test_resolve_sac_bin_in_sif_returns_agent_venv_path() -> None:
    # Arrange — no override
    saved = os.environ.pop("SAC_BIN_IN_SIF", None)
    try:
        # Act
        from scitex_agent_container.runtimes._apptainer_inner_argv import (
            resolve_sac_bin_in_sif,
        )

        path = resolve_sac_bin_in_sif()
        # Assert — the verified in-SIF default. If someone changes this
        # they must verify with `ls /opt/venv-agent/bin/sac` inside the
        # running SIF and update both the constant and this test.
        assert path == "/opt/venv-agent/bin/sac"
    finally:
        if saved is not None:
            os.environ["SAC_BIN_IN_SIF"] = saved


def test_resolve_sac_bin_in_sif_honours_env_override(monkeypatch) -> None:
    # Arrange — operator escape hatch for rebuilt SIFs.
    monkeypatch.setenv("SAC_BIN_IN_SIF", "/custom/path/to/sac")
    # Act
    from scitex_agent_container.runtimes._apptainer_inner_argv import (
        resolve_sac_bin_in_sif,
    )

    path = resolve_sac_bin_in_sif()
    # Assert
    assert path == "/custom/path/to/sac"


def test_tui_channel_config_command_is_resolved_sac_path(tmp_path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_WITH_CHANNEL)
    config = load_config(str(spec))
    # Act
    _, channel_mcp = tui_channel_config(config)
    # Assert — the inline JSON's command is the resolver's value
    # (defaults to /opt/venv-agent/bin/sac for sac-base.sif). The
    # legacy /opt/venv-sac/bin/sac would silently fail exec inside
    # the SIF since the binary does not exist there.
    assert channel_mcp is not None
    assert '"command": "/opt/venv-agent/bin/sac"' in channel_mcp


def test_build_run_argv_appends_credentials_bind_last(tmp_path) -> None:
    # Arrange — designated creds + a real spec.
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text("{}", encoding="utf-8")
    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(extra=f"    credentials_file: {creds}"),
    )
    config = load_config(str(spec))
    # Act
    argv = build_run_argv(
        config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert — the creds bind sits immediately before the sif path so no
    # later home bind can shadow it.
    sif_idx = argv.index("/img/sac.sif")
    assert argv[sif_idx - 1] == f"{creds}:/home/agent/.claude/.credentials.json:rw"
