"""Tests for ``cli_pkg.status_cmds`` (status + health + claude-account block).

PA-306 no-mocks rewrite. The previous version monkeypatched
``status_cmds.Registry`` (with a hand-rolled ``_FakeRegistry``),
``status_cmds.agent_status``, ``status_cmds.load_config``, and
``status_cmds.health_check``. This version exercises real
production collaborators:

* ``Registry`` is redirected to ``tmp_path`` via the documented
  ``SCITEX_AGENT_CONTAINER_REGISTRY_DIR`` env var (same pattern as
  ``test_priority_cmds.py``). The module-level ``REGISTRY_DIR``
  constant is refreshed via ``importlib.reload`` and the
  ``status_cmds.Registry`` symbol is re-bound -- a real callable
  seam, not ``MagicMock``.
* ``agent_status`` runs against a real on-disk registry entry pointing
  at a real ``spec.yaml`` validated by ``scitex-agent-container/v3``.
  With no apptainer/multiplexer running in the test environment, the
  real ``ClaudeSessionRuntime.is_running`` returns ``False`` and the
  status comes back as ``"stopped"`` -- a real, deterministic code
  path.
* ``health_check`` is exercised through the real ``sdk-alive`` method
  on the same kind of real config; honest unhealthy is the only
  branch reachable without a live container.
* Tests that previously asserted on shapes only producible by a mocked
  ``agent_status`` (custom ``agent`` / ``_internal`` keys; scripted
  ``RuntimeError("nope")``) are deleted -- there is no honest way
  to drive the real code into those shapes.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.status_cmds as status_cmds
from scitex_agent_container.cli_pkg.status_cmds import (
    _format_claude_account_block,
    health,
    status,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

# ---------------------------------------------------------------------------
# Real-collaborator fixtures (registry + spec.yaml on disk)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_registry(tmp_path, env_save_restore):
    """Redirect the file-backed registry to ``tmp_path``.

    Reloads ``_state.registry`` so its module-level ``REGISTRY_DIR``
    picks up the env var, then re-binds ``status_cmds.Registry`` to the
    refreshed class. Real reload, no monkeypatch.
    """
    reg = tmp_path / "registry"
    reg.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_REGISTRY_DIR", str(reg))
    import scitex_agent_container._state.registry as _reg

    importlib.reload(_reg)
    # Undo the reload once the env is back — otherwise ``REGISTRY_DIR`` stays
    # pinned at this test's (soon-deleted) tmp dir for the rest of the worker.
    env_save_restore.reload_after_restore(_reg)
    saved = status_cmds.Registry
    status_cmds.Registry = _reg.Registry  # type: ignore[assignment]
    try:
        yield reg
    finally:
        status_cmds.Registry = saved  # type: ignore[assignment]


def _write_spec(parent: Path, name: str, *, body: str | None = None) -> Path:
    """Write a minimal v3-valid spec.yaml at ``<parent>/<name>/spec.yaml``.

    ``name`` is taken from the parent directory by sac's dir-as-SSoT.
    """
    agent_dir = parent / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml(
            body
            if body is not None
            else (
                "apiVersion: scitex-agent-container/v3\n"
                "kind: Agent\n"
                "metadata: {}\n"
                "spec:\n"
                "  runtime: apptainer\n"
                "  host: ${HOSTNAME}\n"
                "  workdir: /home/agent/work\n"
                "  apptainer:\n"
                "    image: /x.sif\n"
                "    binds: []\n"
                "  claude:\n"
                "    model: sonnet\n"
                "  health:\n"
                "    enabled: true\n"
                "    interval: 60\n"
                "  restart:\n"
                "    policy: on-failure\n"
                "    max_retries: 3\n"
            )
        )
    )
    return spec


def _register(reg_dir: Path, name: str, config_path: Path) -> None:
    """Write a real registry JSON entry for ``name`` -> ``config_path``."""
    (reg_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "config": str(config_path),
                "pid": 1,
                "started_at": "2026-01-01T00:00:00Z",
                "screen": name,
            }
        )
    )


# ===========================================================================
# _format_claude_account_block -- pure function, no collaborators
# ===========================================================================


def test_format_account_block_returns_empty_when_all_none():
    # Arrange
    meta = {
        "email_address": None,
        "organization_name": None,
        "display_name": None,
        "billing_type": None,
        "subscription_type": None,
        "rate_limit_tier": None,
        "has_available_subscription": None,
        "has_extra_usage_enabled": None,
        "cached_extra_usage_disabled_reason": None,
        "subscription_created_at": None,
    }
    # Act
    lines = _format_claude_account_block(meta)
    # Assert
    assert lines == []


@pytest.fixture
def _full_meta():
    return {
        "email_address": "x@y.com",
        "organization_name": "Acme",
        "display_name": "Ada",
        "billing_type": "credit-card",
        "subscription_type": "pro",
        "rate_limit_tier": "tier-3",
        "has_available_subscription": True,
        "has_extra_usage_enabled": True,
        "cached_extra_usage_disabled_reason": None,
        "subscription_created_at": "2024-01-01",
    }


def test_format_account_block_renders_email(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    joined = "\n".join(_format_claude_account_block(meta))
    # Assert
    assert "x@y.com" in joined


def test_format_account_block_renders_organization(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    joined = "\n".join(_format_claude_account_block(meta))
    # Assert
    assert "Acme" in joined


def test_format_account_block_renders_subscription_type(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    joined = "\n".join(_format_claude_account_block(meta))
    # Assert
    assert "pro" in joined


def test_format_account_block_renders_rate_limit_tier(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    joined = "\n".join(_format_claude_account_block(meta))
    # Assert
    assert "tier-3" in joined


def test_format_account_block_renders_available_yes_when_true(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    lines = _format_claude_account_block(meta)
    avail_line = next(line for line in lines if "Available" in line)
    # Assert
    assert avail_line.endswith("yes")


def test_format_account_block_renders_extra_enabled_when_true(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    lines = _format_claude_account_block(meta)
    extra_line = next(line for line in lines if "Extra usage" in line)
    # Assert
    assert "enabled" in extra_line


def test_format_account_block_renders_subscription_created_at(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    joined = "\n".join(_format_claude_account_block(meta))
    # Assert
    assert "2024-01-01" in joined


def test_format_account_block_renders_since_in_jst():
    # Arrange — the raw ``...T...Z`` API stamp must render as a readable
    # JST wall clock (operator 2026-07-13), not the unreadable ISO string.
    meta = {
        "email_address": "x@y.com",
        "subscription_created_at": "2025-05-30T19:59:34.010055Z",
    }
    # Act
    lines = _format_claude_account_block(meta)
    since_line = next(line for line in lines if "Since" in line)
    # Assert — 19:59 UTC + 9h = 04:59 JST the next day.
    assert since_line.rstrip().endswith("2025-05-31 04:59 (JST)")


def test_format_account_block_since_missing_renders_dash():
    # Arrange — no subscription_created_at → the Since cell is a bare dash.
    meta = {"email_address": "x@y.com"}
    # Act
    lines = _format_claude_account_block(meta)
    since_line = next(line for line in lines if "Since" in line)
    # Assert
    assert since_line.rstrip().endswith("-")


def test_format_account_block_header_present(_full_meta):
    # Arrange
    meta = _full_meta
    # Act
    joined = "\n".join(_format_claude_account_block(meta))
    # Assert
    assert "Claude Code account" in joined


def test_format_account_block_subscription_unknown_when_both_none():
    # Arrange
    meta = {
        "email_address": "x@y.com",
        "subscription_type": None,
        "rate_limit_tier": None,
    }
    # Act
    lines = _format_claude_account_block(meta)
    sub_line = next(line for line in lines if "Subscription" in line)
    # Assert
    assert sub_line.endswith("-")


def test_format_account_block_available_false_renders_no():
    # Arrange
    meta = {"email_address": "x@y.com", "has_available_subscription": False}
    # Act
    lines = _format_claude_account_block(meta)
    avail_line = next(line for line in lines if "Available" in line)
    # Assert
    assert avail_line.endswith("no")


def test_format_account_block_extra_disabled_says_disabled():
    # Arrange
    meta = {
        "email_address": "x@y.com",
        "has_extra_usage_enabled": False,
        "cached_extra_usage_disabled_reason": "limit-reached",
    }
    # Act
    lines = _format_claude_account_block(meta)
    extra_line = next(line for line in lines if "Extra usage" in line)
    # Assert
    assert "disabled" in extra_line


def test_format_account_block_extra_disabled_includes_reason():
    # Arrange
    meta = {
        "email_address": "x@y.com",
        "has_extra_usage_enabled": False,
        "cached_extra_usage_disabled_reason": "limit-reached",
    }
    # Act
    lines = _format_claude_account_block(meta)
    extra_line = next(line for line in lines if "Extra usage" in line)
    # Assert
    assert "limit-reached" in extra_line


def test_format_account_block_extra_disabled_without_reason_ends_bare():
    # Arrange
    meta = {"email_address": "x@y.com", "has_extra_usage_enabled": False}
    # Act
    lines = _format_claude_account_block(meta)
    extra_line = next(line for line in lines if "Extra usage" in line)
    # Assert
    assert extra_line.rstrip().endswith("disabled")


def test_format_account_block_missing_keys_render_as_dash():
    # Arrange -- only one non-None field; the rest must render as ``-``.
    meta = {"email_address": "x@y.com"}
    # Act
    lines = _format_claude_account_block(meta)
    org_line = next(line for line in lines if "Organization" in line)
    # Assert
    assert org_line.endswith("-")


# ===========================================================================
# show-status -- fleet view (no NAME)
# ===========================================================================


def test_status_terse_without_name_exits_2(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--terse"])
    # Assert
    assert result.exit_code == 2


def test_status_terse_without_name_emits_error_payload(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--terse"])
    payload = json.loads(result.stdout)
    # Assert
    assert "error" in payload and "--terse" in payload["error"]


def test_status_fleet_json_exits_zero(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_status_fleet_json_returns_agents_list(tmp_registry):
    # Arrange -- empty registry, no disk-defined agents.
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # ``result.output`` folds stderr in (click 8.4 dropped mix_stderr), so an
    # ambient WARN logged during config load lands AHEAD of the payload and
    # json.loads dies at char 0. Parse the payload stream only.
    data = json.loads(result.stdout)
    # Assert -- key exists and value is a list (possibly populated by
    # disk-defined agents discovered under ~/.scitex/agent-container).
    assert isinstance(data.get("agents"), list)


def test_status_fleet_json_includes_registered_agent(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "fleet-agent")
    _register(tmp_registry, "fleet-agent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["--json"])
    # See the note above: parse stdout, not the stderr-folded ``output``.
    data = json.loads(result.stdout)
    names = [row["name"] for row in data["agents"]]
    # Assert
    assert "fleet-agent" in names


def test_status_fleet_table_exits_zero(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, [])
    # Assert
    assert result.exit_code == 0


def test_status_fleet_table_includes_registered_agent(tmp_path, tmp_registry):
    # Arrange — a registered agent that is NOT running in the test env
    # (no tmux/container). The DEFAULT view shows only running agents
    # (operator TG 1490-1495), so the full roster is behind ``-v``.
    spec = _write_spec(tmp_path, "table-agent")
    _register(tmp_registry, "table-agent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["-v"])
    # Assert
    assert "table-agent" in result.output


# ===========================================================================
# show-status -- per-agent (NAME provided)
# ===========================================================================


def test_status_per_agent_json_exits_zero(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "myagent")
    _register(tmp_registry, "myagent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["myagent", "--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_status_per_agent_json_returns_agent_name(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "myagent")
    _register(tmp_registry, "myagent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["myagent", "--json"])
    info = json.loads(result.stdout)
    # Assert
    assert info["name"] == "myagent"


def test_status_per_agent_json_reports_stopped_when_no_runtime(tmp_path, tmp_registry):
    # Arrange -- no apptainer container is running in the test env.
    spec = _write_spec(tmp_path, "myagent")
    _register(tmp_registry, "myagent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["myagent", "--json"])
    info = json.loads(result.stdout)
    # Assert
    assert info["status"] == "stopped"


def test_status_per_agent_table_includes_name(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "myagent")
    _register(tmp_registry, "myagent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["myagent"])
    # Assert
    assert "myagent" in result.output


def test_status_per_agent_table_includes_stopped_status(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "myagent")
    _register(tmp_registry, "myagent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["myagent"])
    # Assert
    assert "stopped" in result.output


def test_status_per_agent_table_survives_non_ascii_extensions_on_ascii_stdout(
    tmp_path, tmp_registry
):
    """Bug 2 (sac-fleet-ux-misc-2026-06-24): ``spec.extensions`` is an
    opaque pass-through echoed verbatim into the status table -- real
    agents commonly carry non-ASCII content there (and in pane_text /
    CLAUDE.md snippets, which are harder to control deterministically in
    a test). ``CliRunner(charset="ascii")`` gives ``sys.stdout`` a real
    strict-ASCII ``TextIOWrapper`` -- the same shape a locale-stripped
    container/cron invocation produces -- so ``console.print(table)``
    used to raise ``UnicodeEncodeError`` partway through rendering."""
    # Arrange
    spec = _write_spec(
        tmp_path,
        "unicode-agent",
        body=(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "metadata: {}\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  host: ${HOSTNAME}\n"
            "  workdir: /home/agent/work\n"
            "  apptainer:\n"
            "    image: /x.sif\n"
            "    binds: []\n"
            "  claude:\n"
            "    model: sonnet\n"
            "  health:\n"
            "    enabled: true\n"
            "    interval: 60\n"
            "  restart:\n"
            "    policy: on-failure\n"
            "    max_retries: 3\n"
            "  extensions:\n"
            '    note: "❯ ready"\n'
        ),
    )
    _register(tmp_registry, "unicode-agent", spec)
    runner = CliRunner(charset="ascii")
    # Act
    result = runner.invoke(status, ["unicode-agent"])
    # Assert
    assert result.exit_code == 0, repr(result.exception)


def test_status_per_agent_not_in_registry_json_exits_one(tmp_registry):
    # Arrange -- empty registry; real ``agent_status`` raises
    # ``RuntimeError("Agent 'ghost' not found in registry")``.
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["ghost", "--json"])
    # Assert
    assert result.exit_code == 1


def test_status_per_agent_not_in_registry_json_reports_error(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["ghost", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "not found" in payload.get("error", "")


def test_status_per_agent_not_in_registry_human_exits_one(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["ghost"])
    # Assert
    assert result.exit_code == 1


def test_status_per_agent_not_in_registry_human_reports_error(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["ghost"])
    # Assert
    assert "not found" in result.output or "Error" in result.output


def test_status_per_agent_terse_drops_unwhitelisted_keys(tmp_path, tmp_registry):
    # Arrange -- the real ``agent_status`` payload has keys like
    # ``name`` / ``config`` / ``screen`` / ``hooks_configured`` /
    # ``listen`` / ``extensions`` / ``snapshot`` that are NOT in
    # ``TERSE_STATUS_FIELDS``; ``--terse`` must drop them.
    spec = _write_spec(tmp_path, "terse-agent")
    _register(tmp_registry, "terse-agent", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["terse-agent", "--terse"])
    info = json.loads(result.stdout)
    # Assert -- ``config`` / ``screen`` are real status fields that the
    # terse whitelist deliberately excludes.
    assert "config" not in info and "screen" not in info


def test_status_per_agent_terse_keeps_whitelisted_keys(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "terse-agent2")
    _register(tmp_registry, "terse-agent2", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(status, ["terse-agent2", "--terse"])
    info = json.loads(result.stdout)
    # Assert -- terse projects flat dotted keys from TERSE_STATUS_FIELDS;
    # ``agent`` is always present (value may be ``None`` if the source
    # status dict lacks that exact key).
    assert "agent" in info


# ===========================================================================
# check-health
# ===========================================================================


def test_health_not_in_registry_human_exits_one(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["ghost"])
    # Assert
    assert result.exit_code == 1


def test_health_not_in_registry_human_mentions_name(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["ghost"])
    # Assert
    assert "ghost" in result.output


def test_health_not_in_registry_json_exits_one(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["ghost", "--json"])
    # Assert
    assert result.exit_code == 1


def test_health_not_in_registry_json_emits_error_payload(tmp_registry):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["ghost", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "error" in payload


def test_health_load_config_failure_json_exits_one(tmp_path, tmp_registry):
    # Arrange -- registry points at a malformed YAML; real ``load_config``
    # raises ``ValueError``.
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_spec = bad_dir / "spec.yaml"
    bad_spec.write_text("apiVersion: WRONG/v0\nspec: {}\n")
    _register(tmp_registry, "bad", bad_spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["bad", "--json"])
    # Assert
    assert result.exit_code == 1


def test_health_load_config_failure_json_mentions_validation(tmp_path, tmp_registry):
    # Arrange
    bad_dir = tmp_path / "bad2"
    bad_dir.mkdir()
    bad_spec = bad_dir / "spec.yaml"
    bad_spec.write_text("apiVersion: WRONG/v0\nspec: {}\n")
    _register(tmp_registry, "bad2", bad_spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["bad2", "--json"])
    payload = json.loads(result.stdout)
    # Assert -- production catches the load_config exception and surfaces
    # its message in the ``error`` field.
    assert "validation failed" in payload["error"]


def test_health_unhealthy_json_exits_one(tmp_path, tmp_registry):
    # Arrange -- real spec, real ``sdk-alive`` against a non-running
    # runtime -> ``(False, "unhealthy: SDK runner not running")``.
    spec = _write_spec(tmp_path, "unhealthy")
    _register(tmp_registry, "unhealthy", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["unhealthy", "--json"])
    # Assert
    assert result.exit_code == 1


def test_health_unhealthy_json_reports_unhealthy(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "unhealthy2")
    _register(tmp_registry, "unhealthy2", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["unhealthy2", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["healthy"] is False


def test_health_unhealthy_human_exits_one(tmp_path, tmp_registry):
    # Arrange
    spec = _write_spec(tmp_path, "unhealthy3")
    _register(tmp_registry, "unhealthy3", spec)
    runner = CliRunner()
    # Act
    result = runner.invoke(health, ["unhealthy3"])
    # Assert
    assert result.exit_code == 1
