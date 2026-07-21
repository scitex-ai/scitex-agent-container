"""Integration tests for the OAuth preflight wiring in ``sac agents start``.

These tests drive the real ``start`` click command end-to-end and
verify the preflight at three layers:

1. The preflight fires only when actual dispatch is about to occur —
   argument-validation `sys.exit(2)` paths and the bulk `--yes/-y`
   refusal path BOTH short-circuit before the credentials file is
   ever read. This preserves the long-standing CLI contract that
   misconfigured invocations exit at code 2.
2. When dispatch IS about to fire, an expired credentials file
   exits at code 1 with the helper's message on stderr (no traceback).
3. The ANTHROPIC_API_KEY env var (api-key auth path) bypasses the
   preflight even with an expired credentials file present.

To avoid touching the operator's real ``~/.claude/.credentials.json``,
each test redirects ``$HOME`` at a tmp dir and materialises a fake
credentials file under that tmp dir. ``Path.home()`` (called by the
preflight) reads ``$HOME`` on linux, so the redirect is honoured.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._start import start

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_expired_creds(home: Path) -> Path:
    """Write an expired credentials file under ``$home/.claude/``."""
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    creds = claude_dir / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-fake",
                    "refreshToken": "sk-ant-ort-fake",
                    "expiresAt": 1,  # epoch=1970, ancient history
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return creds


# ---------------------------------------------------------------------------
# Bulk path — preflight must NOT fire before the --yes refusal
# ---------------------------------------------------------------------------


class TestArgumentValidationExitsBeforePreflight:
    """Argument-validation paths keep their exit-code-2 contract even when
    the lead's credentials file is expired."""

    def test_bulk_without_yes_still_exits_two_with_expired_creds(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        agents_dir = tmp_path / "agents"
        (agents_dir / "a").mkdir(parents=True)
        (agents_dir / "a" / "a.yaml").write_text("x")
        (agents_dir / "b").mkdir()
        (agents_dir / "b" / "b.yaml").write_text("x")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir)])
        # Assert
        assert result.exit_code == 2

    def test_resume_with_session_new_session_still_exits_two_with_expired_creds(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        runner = CliRunner()
        # Act
        result = runner.invoke(
            start, ["alpha", "--resume", "abc", "--session", "new-session"]
        )
        # Assert
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Single-target dispatch — preflight fires, exits 1 on expired token
# ---------------------------------------------------------------------------


class TestExpiredCredsBlocksDispatch:
    """When the lead's OAuth token is expired and a real target is
    being dispatched, the CLI must exit 1 with the expiry message on
    stderr — no traceback bubbling up."""

    def test_expired_creds_exits_one_for_single_target(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["any-target-name"])
        # Assert
        assert result.exit_code == 1

    def test_expired_creds_error_message_mentions_claude_login(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["any-target-name"], catch_exceptions=False)
        # Assert
        assert "claude login" in result.output

    def test_expired_creds_does_not_raise_unhandled_traceback(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["any-target-name"], catch_exceptions=False)
        # Assert
        assert result.exception is None or isinstance(result.exception, SystemExit)


# ---------------------------------------------------------------------------
# API-key bypass — preflight does not fire when ANTHROPIC_API_KEY is set
# ---------------------------------------------------------------------------


class TestApiKeyBypassesPreflight:
    """ANTHROPIC_API_KEY in env routes the SDK to the api-key auth
    path; the OAuth credentials file is irrelevant and the preflight
    must not block. Test by setting an expired credentials file AND
    the env var — the CLI should NOT exit on the preflight branch.
    Downstream agent_start may still fail (no real workspace exists),
    but the failure must come from there, not from the preflight."""

    def test_api_key_env_bypasses_expired_creds_preflight(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.set("ANTHROPIC_API_KEY", "sk-ant-fake")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["any-target-name"])
        # Assert: NOT 1 with "claude login" — preflight didn't run.
        assert "claude login" not in result.output


# ---------------------------------------------------------------------------
# PR#314: --broker-self + provider-backed specs skip the parent's OAuth
# check (lead msg 24a8b27c, clew Spartan dogfood 2026-06-06). The parent
# in either orchestrator-only mode never talks to Anthropic, so checking
# its OAuth credentials is spurious — and (per clew's Spartan repro) it
# blocked the broker boot on a 4.6-day-old expired creds file. Both
# escape hatches are surgical: any Anthropic-backed target still
# triggers the check.
# ---------------------------------------------------------------------------


def _install_provider_spec(
    yaml_dir: Path, name: str, *, base_url: str = "http://127.0.0.1:4000"
) -> Path:
    """Write a minimal v3 spec.yaml with spec.claude.provider configured.

    The spec is just enough to load_config + reach the preflight
    branch's provider-non-None check; downstream dispatch will fail
    on the missing image/runtime but that runs AFTER the preflight.
    """
    agent_dir = yaml_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        "  workdir: /home/agent/work\n"
        "  claude:\n"
        "    model: deepseek-chat\n"
        "    provider:\n"
        f"      base_url: {base_url}\n"
        "      auth_token_env: FAKE_TOKEN_ENV\n"
        "  apptainer:\n"
        "    image: /nonexistent/dummy.sif\n"
        "    binds: []\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"),
        encoding="utf-8",
    )
    return spec


def _install_anthropic_spec(yaml_dir: Path, name: str) -> Path:
    """Write a minimal v3 spec.yaml WITHOUT a provider (Anthropic path)."""
    agent_dir = yaml_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  host: ${HOSTNAME}\n"
        "  workdir: /home/agent/work\n"
        "  claude:\n    model: sonnet\n"
        "  apptainer:\n"
        "    image: /nonexistent/dummy.sif\n"
        "    binds: []\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"),
        encoding="utf-8",
    )
    return spec


class TestBrokerSelfBypassesPreflight:
    """--broker-self is the orchestrator-only flag (PR#311). The parent
    process never talks to Anthropic — it bootstraps a local listen
    and spawns the capsule, which gets its own preflight in the
    broker's child subprocess. Skipping the parent's OAuth check is
    the durable fix for the Spartan-creds-expiry treadmill that
    repeatedly bit clew's cohort-A launches."""

    def test_broker_self_skips_oauth_preflight_on_expired_creds(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange — expired creds + --broker-self. Pre-PR#314 the
        # preflight blocked with "claude login"; post-PR#314 it skips.
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["any-target-name", "--broker-self"])
        # Assert — preflight skipped (no "claude login" in output).
        assert "claude login" not in result.output


class TestProviderBackedSpecBypassesPreflight:
    """When EVERY target's spec.claude.provider is non-None, the SDK
    routes through a non-Anthropic backend (LiteLLM, vLLM, DeepSeek,
    gateway via ANTHROPIC_BASE_URL). The bind-mounted Anthropic
    credentials are never read, so the preflight is moot."""

    def test_provider_backed_target_skips_oauth_preflight(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange — expired creds + a provider-backed spec on disk.
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        yaml_dir = tmp_path / "agents"
        _install_provider_spec(yaml_dir, "provider-agent")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_dir))
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["provider-agent"])
        # Assert — preflight skipped (no "claude login" in output);
        # the downstream dispatch may still fail on the dummy.sif but
        # that comes from agent_start, not the preflight.
        assert "claude login" not in result.output

    def test_anthropic_backed_target_still_runs_oauth_preflight(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange — expired creds + a spec WITHOUT provider. The
        # preflight MUST still fire (regression guard: don't accidentally
        # skip all preflights when at least one spec needs OAuth).
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        yaml_dir = tmp_path / "agents"
        _install_anthropic_spec(yaml_dir, "anthropic-agent")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_dir))
        runner = CliRunner()
        # Act
        result = runner.invoke(start, ["anthropic-agent"])
        # Assert — preflight DID fire (the existing Anthropic path is unchanged).
        assert "claude login" in result.output

    def test_unresolvable_target_defaults_to_running_oauth_preflight(
        self, tmp_path: Path, env_save_restore
    ) -> None:
        # Arrange — defensive default: if a spec can't be loaded,
        # the preflight still fires (better to surface the OAuth
        # state than to silently skip on a broken spec).
        env_save_restore.set("HOME", str(tmp_path))
        env_save_restore.delete("ANTHROPIC_API_KEY")
        env_save_restore.delete("SAC_ANTHROPIC_API_KEY")
        _install_expired_creds(tmp_path)
        runner = CliRunner()
        # Act — target name doesn't resolve to anything on disk.
        result = runner.invoke(start, ["nonexistent-target-9c4f7a"])
        # Assert — preflight fired (defensive); operator sees the
        # OAuth message before they get the spec-resolve error.
        assert "claude login" in result.output
