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
import shlex
import socket
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container.config import load_config
from scitex_agent_container.runtimes._apptainer_auth import (
    CredentialExpiredError,
    credentials_file_bind,
    ensure_credentials_bind_target,
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
{extra}
"""


@pytest.fixture
def tui_config(tmp_path):
    spec = _write_spec(tmp_path, _BASE_SPEC.format(extra=""))
    return load_config(str(spec))


def _effective_inner_argv(inner: list[str]) -> list[str]:
    """Token-list of the actual ``claude`` invocation.

    ``build_inner_argv`` now ALWAYS wraps the runner argv in
    ``/bin/bash -lc <inline>`` (the unconditional SAC_GIT_* env alias
    step — see ``_apptainer_inner_argv._GIT_ENV_ALIAS_STEPS``), so the
    literal inner command is shell-quoted text after ``exec `` inside
    ``inner[2]`` rather than being ``inner`` itself.
    """
    exec_part = inner[2].split("exec ", 1)[1]
    return shlex.split(exec_part)


# ---------------------------------------------------------------------------
# build_inner_argv(tui=True) — interactive claude as the inner process
# ---------------------------------------------------------------------------


def test_tui_inner_argv_runs_claude_binary(tui_config) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    inner = _effective_inner_argv(build_inner_argv(tui_config, tui=True))
    # Assert — the inner command is the interactive claude TUI.
    assert inner[0] == "claude"


def test_tui_inner_argv_threads_model_flag(tui_config) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    inner = _effective_inner_argv(build_inner_argv(tui_config, tui=True))
    # Assert
    assert "--model" in inner and "claude-opus-4-8[1m]" in inner


def test_tui_inner_argv_appends_spec_flags(tui_config) -> None:
    # Arrange — config from the tui_config fixture.
    # Act
    inner = _effective_inner_argv(build_inner_argv(tui_config, tui=True))
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


def test_credentials_file_bind_mounts_designated_file_ro(tmp_path) -> None:
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
    # Assert — READ-ONLY bind onto the canonical container creds path
    # (master-host single-refresher model: the agent never refreshes).
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
    # Assert — single-file :rw bind onto the canonical container creds path,
    # source = the per-host snapshot. NO copy, NO CLAUDE_CONFIG_DIR redirect.
    # READ-ONLY: the agent reads the snapshot; the host timer refreshes it.
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
    # Assert — explicit file path is the source, bound READ-ONLY.
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
  host: local
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
    model: sonnet
    channels:
      - server:sac
"""


_SPEC_WITH_TELEGRAMMER_AND_PORT = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
spec:
  runtime: tui
  workdir: /tmp/agt-work
  host: local
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
    model: sonnet
    channels:
      - server:claude-code-telegrammer
  a2a:
    port: 19007
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


def test_build_run_argv_tui_wires_telegrammer_wake_env(
    tmp_path, listen_bearer_token
) -> None:
    # Arrange — a TUI agent requesting its own telegrammer channel with a
    # resolved a2a port (as resolve_a2a_port sets at agent_start). SDK parity:
    # the SDK injects the wake via mcp_servers; the TUI forwards it as a
    # container --env the telegrammer inherits (same path as its bot token via
    # --env-file), so an inbound message POSTs to /v1/turn and wakes an idle
    # session — the SDK<->TUI drift this closes.
    spec = _write_spec(tmp_path, _SPEC_WITH_TELEGRAMMER_AND_PORT)
    config = load_config(str(spec))
    state_dir = tmp_path / "state"
    (state_dir / "home").mkdir(parents=True)
    # Act
    argv = build_run_argv(
        config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert
    assert (
        "--env CLAUDE_CODE_TELEGRAMMER_TURN_URL=http://127.0.0.1:19007/v1/turn"
        in " ".join(argv)
    )


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


# ---------------------------------------------------------------------------
# ensure_credentials_bind_target — apptainer file-bind needs a pre-existing
# destination (fresh-agent boot FATAL fix)
# ---------------------------------------------------------------------------


def _valid_creds_file(tmp_path: Path) -> Path:
    creds = tmp_path / "acct" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(
        '{"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9999999999000}}',
        encoding="utf-8",
    )
    return creds


def test_ensure_bind_target_noop_when_no_credentials(tui_config, tmp_path) -> None:
    # Arrange — config has no credentials_file / account (the tui_config
    # fixture); no bind will be emitted, so nothing to pre-create.
    home_host = tmp_path / "home"
    home_host.mkdir()
    # Act
    result = ensure_credentials_bind_target(tui_config, home_host=home_host)
    # Assert
    assert result is None


def test_ensure_bind_target_creates_placeholder_in_workspace_home(tmp_path) -> None:
    # Arrange — designated creds, default (non-overlay) spec → the container
    # $HOME is backed by the workspace-home bind.
    creds = _valid_creds_file(tmp_path)
    spec = _write_spec(
        tmp_path, _BASE_SPEC.format(extra=f"    credentials_file: {creds}")
    )
    config = load_config(str(spec))
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    # Act
    placeholder = ensure_credentials_bind_target(config, home_host=home_host)
    # Assert — an empty placeholder now exists at the workspace-home path.
    assert placeholder == home_host / ".claude" / ".credentials.json"


def test_ensure_bind_target_placeholder_is_a_real_file_on_disk(tmp_path) -> None:
    # Arrange
    creds = _valid_creds_file(tmp_path)
    spec = _write_spec(
        tmp_path, _BASE_SPEC.format(extra=f"    credentials_file: {creds}")
    )
    config = load_config(str(spec))
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    # Act
    ensure_credentials_bind_target(config, home_host=home_host)
    # Assert — apptainer's file-bind destination now pre-exists.
    assert (home_host / ".claude" / ".credentials.json").is_file()


def test_ensure_bind_target_prefers_overlay_upper_home(tmp_path) -> None:
    # Arrange — a relaxed directory-overlay spec: the container $HOME is the
    # overlay upper-home (bound OVER --home), NOT the workspace-home. The
    # placeholder MUST land in the upper-home so the bind destination exists.
    creds = _valid_creds_file(tmp_path)
    spec = _write_spec(
        tmp_path, _BASE_SPEC.format(extra=f"    credentials_file: {creds}")
    )
    config = load_config(str(spec))
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    upper_home = tmp_path / "overlay" / "upper" / "home" / "agent"
    upper_home.mkdir(parents=True)
    # Act
    placeholder = ensure_credentials_bind_target(
        config, home_host=home_host, overlay_upper_home=upper_home
    )
    # Assert — placeholder is under the overlay upper-home, not workspace-home.
    assert placeholder == upper_home / ".claude" / ".credentials.json"


def test_ensure_bind_target_falls_back_to_workspace_home_when_upper_absent(
    tmp_path,
) -> None:
    # Arrange — an overlay_upper_home that does NOT exist on disk (e.g. an
    # .img overlay we cannot write into): the helper must fall back to the
    # workspace-home bind rather than create an upper-home that won't be used.
    creds = _valid_creds_file(tmp_path)
    spec = _write_spec(
        tmp_path, _BASE_SPEC.format(extra=f"    credentials_file: {creds}")
    )
    config = load_config(str(spec))
    home_host = tmp_path / "state" / "home"
    home_host.mkdir(parents=True)
    missing_upper = tmp_path / "no-such-upper"  # never created
    # Act
    placeholder = ensure_credentials_bind_target(
        config, home_host=home_host, overlay_upper_home=missing_upper
    )
    # Assert
    assert placeholder == home_host / ".claude" / ".credentials.json"


def test_ensure_bind_target_does_not_overwrite_existing_placeholder(tmp_path) -> None:
    # Arrange — a placeholder that already carries content (e.g. a prior
    # boot's real credential); the helper must leave it untouched (the bind
    # shadows it anyway, and we never want to truncate a real file).
    creds = _valid_creds_file(tmp_path)
    spec = _write_spec(
        tmp_path, _BASE_SPEC.format(extra=f"    credentials_file: {creds}")
    )
    config = load_config(str(spec))
    home_host = tmp_path / "state" / "home"
    (home_host / ".claude").mkdir(parents=True)
    existing = home_host / ".claude" / ".credentials.json"
    existing.write_text("PRIOR", encoding="utf-8")
    # Act
    ensure_credentials_bind_target(config, home_host=home_host)
    # Assert
    assert existing.read_text(encoding="utf-8") == "PRIOR"


def test_build_run_argv_precreates_credentials_bind_target(tmp_path) -> None:
    # Arrange — full build with designated creds; the host placeholder must
    # exist AFTER build so the emitted file-bind destination pre-exists (the
    # fresh-overlay-agent boot FATAL this fix closes).
    creds = _valid_creds_file(tmp_path)
    spec = _write_spec(
        tmp_path, _BASE_SPEC.format(extra=f"    credentials_file: {creds}")
    )
    config = load_config(str(spec))
    state_dir = tmp_path / "state"
    # Act
    build_run_argv(config, state_dir=state_dir, sif_path=Path("/img/sac.sif"), tui=True)
    # Assert — placeholder at the workspace-home backing the /home/agent bind.
    assert (state_dir / "home" / ".claude" / ".credentials.json").is_file()


# ---------------------------------------------------------------------------
# spec.access — host-access posture (operator directive 2026-06-19)
#
# DEFAULT ``full``: bind the operator's whole home + open --pwd at the
# canonical workdir path. ``capsule``: only explicit binds + the /work
# alias + --pwd /work (legacy leak-prevention behaviour). The ``_BASE_SPEC``
# carries no ``access`` field, so the default-access tests prove the
# back-compat default is ``full``.
# ---------------------------------------------------------------------------


_ACCESS_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  access: full
  workdir: /home/ywatanabe/proj/figrecipe
  claude:
    model: claude-opus-4-8[1m]
"""


_EXPLICIT_FULL_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: t
    sac-builtin: "off"
spec:
  runtime: tui
  host: local
  workdir: /home/tester/proj/figrecipe
  apptainer:
    image: /x.sif
    binds:
      - /home/tester:/home/tester:rw
  health:
    enabled: true
    interval: 60
  restart:
    policy: on-failure
    max_retries: 3
  claude:
    model: claude-opus-4-8[1m]
"""


def test_access_field_is_rejected_loud(tmp_path) -> None:
    # Arrange — a spec still carrying the REMOVED `access:` knob.
    spec = _write_spec(tmp_path, _ACCESS_SPEC)
    # Act
    ctx = pytest.raises(ValueError, match="spec.access has been REMOVED")
    # Assert — load fails loud with the explicit-binds replacement hint.
    with ctx:
        load_config(str(spec))


def test_build_run_argv_pwd_is_workdir(tui_config, tmp_path) -> None:
    # Arrange — _BASE_SPEC workdir is /tmp/agt-work; --pwd is exactly that.
    # Act
    argv = build_run_argv(
        tui_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert — workdir IS the cwd; no canonical/alias rewrite.
    assert argv[argv.index("--pwd") + 1] == "/tmp/agt-work"


def test_build_run_argv_emits_no_implicit_whole_home_bind(
    tui_config, tmp_path, _isolate_home
) -> None:
    # Arrange — _BASE_SPEC lists no binds, so NOTHING host-broad mounts
    # (the removed `access: full` would have auto-bound the whole home).
    # Act
    argv = build_run_argv(
        tui_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert
    assert f"{_isolate_home}:{_isolate_home}:rw" not in argv


def test_build_run_argv_emits_no_implicit_work_alias(tui_config, tmp_path) -> None:
    # Arrange — no `/work` alias is injected; the workdir is only the --pwd.
    # Act
    argv = build_run_argv(
        tui_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert — no bind maps anything to a `/work` target.
    assert not [a for a in argv if isinstance(a, str) and a.endswith(":/work")]


def test_build_run_argv_explicit_home_bind_passes_through(tmp_path) -> None:
    # Arrange — a "full" agent now declares the whole-home bind EXPLICITLY.
    spec = _write_spec(tmp_path, _EXPLICIT_FULL_SPEC)
    config = load_config(str(spec))
    # Act
    argv = build_run_argv(
        config, state_dir=tmp_path / "state", sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert — the bind appears verbatim (what the spec lists is what mounts).
    assert "/home/tester:/home/tester:rw" in argv


def test_build_run_argv_explicit_full_pwd_opens_at_workdir(tmp_path) -> None:
    # Arrange — same explicit-binds full agent; --pwd opens at its workdir.
    spec = _write_spec(tmp_path, _EXPLICIT_FULL_SPEC)
    config = load_config(str(spec))
    # Act
    argv = build_run_argv(
        config, state_dir=tmp_path / "state", sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert
    assert argv[argv.index("--pwd") + 1] == "/home/tester/proj/figrecipe"


# ---------------------------------------------------------------------------
# spec.apptainer.nested_build — solver builds/pulls its capsule env nested
# (verified 2026-06-20 inside sac-scitex.sif). The flag set needs host
# /dev/fuse (fail-loud otherwise), so the ON assertions skipif it's absent.
# ---------------------------------------------------------------------------


# _BASE_SPEC already carries an ``apptainer:`` block, so add nested_build INTO
# it rather than appending a second ``apptainer:`` key (YAML would drop one).
_NESTED_SPEC = _BASE_SPEC.format(extra="").replace(
    "    binds: []\n",
    "    binds: []\n    nested_build: true\n",
)

_NO_FUSE_SKIP = pytest.mark.skipif(
    not Path("/dev/fuse").exists(), reason="nested_build needs host /dev/fuse"
)


def test_build_run_argv_no_nested_build_omits_dev_fuse(tui_config, tmp_path) -> None:
    # Arrange — _BASE_SPEC carries no apptainer.nested_build.
    # Act
    argv = build_run_argv(
        tui_config,
        state_dir=tmp_path / "state",
        sif_path=Path("/img/sac.sif"),
        tui=True,
    )
    # Assert — no /dev/fuse bind when the knob is off.
    assert "/dev/fuse" not in argv


@_NO_FUSE_SKIP
def test_build_run_argv_nested_build_binds_dev_fuse(tmp_path) -> None:
    # Arrange — a nested_build agent.
    spec = _write_spec(tmp_path, _NESTED_SPEC)
    config = load_config(str(spec))
    # Act
    argv = build_run_argv(
        config, state_dir=tmp_path / "state", sif_path=Path("/img/sac.sif"), tui=True
    )
    # Assert
    assert "/dev/fuse" in argv


@_NO_FUSE_SKIP
def test_build_run_argv_nested_build_masks_subuid(tmp_path) -> None:
    # Arrange
    spec = _write_spec(tmp_path, _NESTED_SPEC)
    config = load_config(str(spec))
    # Act
    joined = " ".join(
        build_run_argv(
            config,
            state_dir=tmp_path / "state",
            sif_path=Path("/img/sac.sif"),
            tui=True,
        )
    )
    # Assert — the /etc/subuid mask rides through to the full argv.
    assert ":/etc/subuid" in joined
