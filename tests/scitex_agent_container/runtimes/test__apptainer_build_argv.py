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
STX-TQ002 AAA-marker + STX-TQ007 one-assert + PA-306 no-mock-fixtures.

The file is named ``test__apptainer_build_argv.py`` to satisfy the
PS-204 §2 orphan-test mirror rule against
``src/scitex_agent_container/runtimes/_apptainer_build_argv.py`` (the
production entry-point under test). The companion helpers tested here
(``_apptainer_auth.credentials_file_bind`` and
``_apptainer_inner_argv.{build_inner_argv,tui_channel_config,
resolve_sac_bin_in_sif}``) are exercised through ``build_run_argv``
to keep the mirror 1:1.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_auth import (
    CredentialExpiredError,
    credentials_file_bind,
)
from scitex_agent_container.runtimes._apptainer_build_argv import build_run_argv
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    build_inner_argv,
    tui_channel_config,
)


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    """Redirect ``HOME`` (and ``Path.home``) to a per-test tmp dir.

    The bearer-token resolver (``_apptainer_build._listen_token_path`` →
    ``_listen.tokens.default_token_path``) anchors on ``Path.home()``. By
    sliding ``HOME`` to ``tmp_path`` we keep the materialised token file
    contained inside the test and never touch the developer's real
    ``~/.scitex/agent-container/tokens/`` tree.
    """
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def listen_bearer_token(_isolate_home: Path) -> Path:
    """Materialise a real ``sac listen`` bearer token at the resolver path.

    Tests that exercise ``build_run_argv`` against a spec whose
    ``spec.claude.channels`` includes ``server:sac`` would otherwise hit
    the fail-loud guard in
    ``_apptainer_listen_env.listen_env_flags`` — that guard exists to
    refuse launches whose in-container channel adapter can never
    authenticate to ``sac listen``. CI hosts have no token file, so the
    guard turns an argv-shape test into a RuntimeError. This fixture
    writes a real (non-empty) token to the resolver-computed path so the
    guard sees a usable bearer and ``build_run_argv`` proceeds.

    The path layout mirrors the production resolver
    (``_listen.tokens.default_token_path``): ``$HOME/.scitex/agent-
    container/tokens/listen-<hostname>.token``. We use ``socket.
    gethostname()`` here so a CI runner rename does not desync the
    fixture from the resolver.
    """
    home = _isolate_home
    token_dir = home / ".scitex" / "agent-container" / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / f"listen-{socket.gethostname()}.token"
    token_path.write_text("test-bearer-token-not-a-secret\n", encoding="utf-8")
    return token_path


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
    sac-builtin: "off"
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
# build_run_argv — to_home/.env injected via --env-file (sac respects .env)
# ---------------------------------------------------------------------------


def test_build_run_argv_injects_env_file_when_present(tui_config, tmp_path) -> None:
    # Arrange — the $HOME/.env that deploy_to_home materialises, at the host
    # side of the /home/agent bind (state_dir/home/.env).
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True, exist_ok=True)
    (state_dir / "home" / ".env").write_text(
        "CLAUDE_CODE_TELEGRAMMER_TELEGRAM_BOT_TOKEN=123:not-a-real-secret\n",
        encoding="utf-8",
    )
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert
    assert "--env-file" in argv and str(state_dir / "home" / ".env") in argv


def test_build_run_argv_omits_env_file_when_absent(tui_config, tmp_path) -> None:
    # Arrange — no .env materialised under state_dir/home.
    state_dir = tmp_path / "state"
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert
    assert "--env-file" not in argv


def test_build_run_argv_env_file_precedes_curated_env(tui_config, tmp_path) -> None:
    # Arrange — .env present; a curated --env (state-db) must be emitted
    # AFTER the --env-file so it wins on conflict (precedence by position).
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True, exist_ok=True)
    (state_dir / "home" / ".env").write_text("FOO=bar\n", encoding="utf-8")
    # Act
    argv = build_run_argv(
        tui_config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert
    assert argv.index("--env-file") < argv.index(
        "SCITEX_AGENT_CONTAINER_STATE_DB=/state/state.db"
    )


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
    # Arrange — a designated credentials file on disk carrying a valid,
    # unexpired OAuth token (the bind now fails loud on a stale or
    # unverifiable pinned credential — see the expiry tests below).
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        '{"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9999999999000}}',
        encoding="utf-8",
    )
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


def test_credentials_file_bind_resolves_account_when_no_explicit_file(
    tmp_path: Path, _isolate_home: Path
) -> None:
    # Arrange — write a real snapshot at the user-scope account-store path,
    # then declare ``spec.claude.account`` (no explicit ``credentials_file:``).
    # ``_isolate_home`` redirects ``HOME`` so the store lands inside
    # ``tmp_path`` and ``_store_path`` resolves to it. Multi-host
    # canonical-single-source model (operator+lead 2026-06-15,
    # lead-learnings/29): each host writable-binds its OWN local
    # snapshot — no copy between hosts.
    acct = "scitex-todo"
    store = _isolate_home / ".scitex" / "agent-container" / "accounts" / acct
    store.mkdir(parents=True, exist_ok=True)
    snap = store / ".credentials.json"
    snap.write_text(
        '{"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9999999999000}}',
        encoding="utf-8",
    )
    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(extra=f"    account: {acct}"),
    )
    config = load_config(str(spec))
    # Act
    flags = credentials_file_bind(config)
    # Assert — single-file rw bind onto the canonical container creds path,
    # source = the per-host snapshot. NO copy, NO CLAUDE_CONFIG_DIR redirect.
    assert flags == [
        "--bind",
        f"{snap}:/home/agent/.claude/.credentials.json:rw",
    ]


def test_credentials_file_bind_explicit_file_wins_over_account(
    tmp_path: Path, _isolate_home: Path
) -> None:
    # Arrange — both ``account:`` and ``credentials_file:`` set. Explicit
    # wins (operator override always trumps auto-resolution).
    acct = "scitex-todo"
    store = _isolate_home / ".scitex" / "agent-container" / "accounts" / acct
    store.mkdir(parents=True, exist_ok=True)
    snap = store / ".credentials.json"
    snap.write_text(
        '{"claudeAiOauth": {"accessToken": "snap", "expiresAt": 9999999999000}}',
        encoding="utf-8",
    )
    explicit = tmp_path / "explicit" / ".credentials.json"
    explicit.parent.mkdir(parents=True, exist_ok=True)
    explicit.write_text(
        '{"claudeAiOauth": {"accessToken": "explicit", "expiresAt": 9999999999000}}',
        encoding="utf-8",
    )
    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(
            extra=f"    account: {acct}\n    credentials_file: {explicit}"
        ),
    )
    config = load_config(str(spec))
    # Act
    flags = credentials_file_bind(config)
    # Assert — explicit file path is the source.
    assert flags == [
        "--bind",
        f"{explicit}:/home/agent/.claude/.credentials.json:rw",
    ]


def test_credentials_file_bind_account_pinned_raises_on_missing_snapshot(
    tmp_path: Path, _isolate_home: Path
) -> None:
    # Arrange — ``account:`` set but NO snapshot at the resolver's path.
    # ``resolve_cred_file`` is the canonical fail-loud point; verify the
    # exception propagates so ``sac agents start`` aborts at config-build
    # time (not silently launches with an unverifiable credential).
    from scitex_agent_container.runtimes._apptainer_creds import (
        PinnedAccountError,
    )

    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(extra="    account: nonexistent-account"),
    )
    config = load_config(str(spec))
    # Act
    # Assert
    with pytest.raises(PinnedAccountError, match=r"nonexistent-account"):
        credentials_file_bind(config)


def test_credentials_file_bind_designated_expired_token_fails_loud(
    tmp_path: Path,
) -> None:
    # Arrange — designate a credentials file whose OAuth token expired in
    # the past relative to the injected ``now``. Before this guard the
    # expired file was bound :rw and the in-container claude 401'd and
    # exited, surfacing only as the opaque empty-pane start failure.
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        '{"claudeAiOauth": {"accessToken": "stale", "expiresAt": 1700000000000}}',
        encoding="utf-8",
    )
    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(extra=f"    credentials_file: {creds}"),
    )
    config = load_config(str(spec))
    # Act — ``now`` (2027) is well after the token's 2023 expiry.
    # Assert
    with pytest.raises(CredentialExpiredError, match=r"expired"):
        credentials_file_bind(config, now=1_800_000_000.0)


def test_credentials_file_bind_designated_missing_expiry_fails_loud(
    tmp_path: Path,
) -> None:
    # Arrange — designate a credentials file with no numeric expiresAt;
    # the token cannot be verified fresh, so the launch must abort loud
    # rather than bind an unverifiable credential.
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        '{"claudeAiOauth": {"accessToken": "no-expiry"}}', encoding="utf-8"
    )
    spec = _write_spec(
        tmp_path,
        _BASE_SPEC.format(extra=f"    credentials_file: {creds}"),
    )
    config = load_config(str(spec))
    # Act
    # Assert
    with pytest.raises(CredentialExpiredError, match=r"unverifiable"):
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


def test_tui_channel_config_sac_subscriber_resolves_across_venvs(tmp_path) -> None:
    # Arrange — a server:sac channel spec.
    spec = _write_spec(tmp_path, _SPEC_WITH_CHANNEL)
    config = load_config(str(spec))
    # Act
    _, channel_mcp = tui_channel_config(config)
    # Assert — the subscriber command is a spawn-time /bin/sh resolver
    # that tries BOTH known venv locations (robust to the SIF layout flip
    # that silently broke server:sac on 2026-06-17), not a single
    # hardcoded path that goes stale on the next image rebuild.
    assert (
        channel_mcp is not None
        and '"command": "/bin/sh"' in channel_mcp
        and "/opt/venv-sac/bin/sac" in channel_mcp
        and "/opt/venv-agent/bin/sac" in channel_mcp
    )


def test_build_run_argv_tui_adds_dev_channels_flag(
    tmp_path, listen_bearer_token
) -> None:
    # Arrange — ``listen_bearer_token`` materialises a real bearer at the
    # resolver path so the ``server:sac`` channel guard
    # (``_apptainer_listen_env.listen_env_flags``) does not refuse the
    # launch on a CI host that has never run ``sac listen``.
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


def test_build_run_argv_tui_injects_channel_subscriber_mcp(
    tmp_path, listen_bearer_token
) -> None:
    # Arrange — ``listen_bearer_token`` materialises a real bearer at the
    # resolver path so the ``server:sac`` channel guard does not refuse
    # this launch on a CI host that has never run ``sac listen``.
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
    # The command resolves sac's in-SIF path at SPAWN time via /bin/sh
    # across the known venv candidates (robust to SIF layout — a single
    # hardcode silently broke server:sac when sac-base.sif moved it from
    # /opt/venv-agent to /opt/venv-sac, 2026-06-17). The resolver must
    # reference the current sac-base.sif location and still drive
    # `sac ... channel`. A bare `"command": "sac"` would silent-exec-fail
    # under --containall (PR #407 class); the absolute candidates avoid it.
    joined = " ".join(argv)
    assert "/opt/venv-sac/bin/sac" in joined and '"channel"' in joined


def test_resolve_sac_bin_in_sif_returns_sac_venv_path() -> None:
    # Arrange — no override
    saved = os.environ.pop("SAC_BIN_IN_SIF", None)
    try:
        # Act
        from scitex_agent_container.runtimes._apptainer_inner_argv import (
            resolve_sac_bin_in_sif,
        )

        path = resolve_sac_bin_in_sif()
        # Assert — the verified current sac-base.sif location (2026-06-17:
        # `ls /opt/venv-{agent,sac}/bin/sac` → only venv-sac exists). The
        # channel MCP itself resolves across candidates at spawn time;
        # this single-path helper just must not point at a phantom path.
        assert path == "/opt/venv-sac/bin/sac"
    finally:
        if saved is not None:
            os.environ["SAC_BIN_IN_SIF"] = saved


def test_resolve_sac_bin_in_sif_honours_env_override() -> None:
    # Arrange — operator escape hatch for rebuilt SIFs. ``monkeypatch``
    # is forbidden by the PA-306 §3 no-mocks rule, so save/restore the
    # env var directly on ``os.environ`` (real state mutation, not a
    # pytest fixture wrapper).
    saved = os.environ.get("SAC_BIN_IN_SIF")
    os.environ["SAC_BIN_IN_SIF"] = "/custom/path/to/sac"
    try:
        # Act
        from scitex_agent_container.runtimes._apptainer_inner_argv import (
            resolve_sac_bin_in_sif,
        )

        path = resolve_sac_bin_in_sif()
    finally:
        if saved is None:
            os.environ.pop("SAC_BIN_IN_SIF", None)
        else:
            os.environ["SAC_BIN_IN_SIF"] = saved
    # Assert
    assert path == "/custom/path/to/sac"


def test_tui_channel_config_command_is_resolved_sac_path(tmp_path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_WITH_CHANNEL)
    config = load_config(str(spec))
    # Act
    _, channel_mcp = tui_channel_config(config)
    # Assert — the inline JSON's command is the /bin/sh spawn-time
    # resolver, NOT a single hardcoded venv path (the
    # /opt/venv-agent/bin/sac hardcode silently broke server:sac on
    # 2026-06-17 when sac-base.sif shipped sac under /opt/venv-sac).
    # The resolver exec's the first executable candidate inside the SIF.
    # ``"" in None`` would TypeError, so the None-check is embedded.
    assert '"command": "/bin/sh"' in (channel_mcp or "")


def test_build_run_argv_appends_credentials_bind_last(tmp_path) -> None:
    # Arrange — designated creds (valid, unexpired) + a real spec.
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        '{"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9999999999000}}',
        encoding="utf-8",
    )
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
