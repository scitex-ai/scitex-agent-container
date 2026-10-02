"""CLI contract for the explicit, default-dry-run owned restore verb."""

from click.testing import CliRunner

from scitex_agent_container.cli_pkg import _agents_restore_worktree as implementation
from scitex_agent_container.cli_pkg.agent_group import agent_group
from scitex_agent_container.config import AgentConfig


def test_restore_verb_exposes_exact_commit_and_reviewed_receipt():
    result = CliRunner().invoke(agent_group, ["restore-worktree", "--help"])
    assert result.exit_code == 0, result.output
    assert "--expected-tip" in result.output
    assert "--receipt-sha256" in result.output
    assert "--apply" in result.output


def test_apply_missing_receipt_is_refused_before_loading_identity(monkeypatch):
    def unexpected_load(*args):
        raise AssertionError("must refuse before reading a spec")

    monkeypatch.setattr(implementation, "load_config", unexpected_load)
    result = CliRunner().invoke(
        agent_group,
        ["restore-worktree", "scitex-scholar", "--expected-tip", "1" * 40, "--apply"],
    )
    assert result.exit_code == 1
    assert "--receipt-sha256" in result.output


def test_default_cli_propagates_exact_identity_and_commit(monkeypatch):
    config = AgentConfig(name="scitex-scholar", workdir="/retained/repo")
    monkeypatch.setattr(implementation, "load_config", lambda path: config)
    monkeypatch.setattr(
        implementation, "resolve_with_prefix", lambda name: "/spec.yaml"
    )
    calls = []

    def restore(cfg, **kwargs):
        calls.append((cfg, kwargs))
        return {"mode": "dry-run", "receipt_sha256": "2" * 64}

    monkeypatch.setattr(implementation, "restore_owned_task_worktree", restore)
    result = CliRunner().invoke(
        agent_group, ["restore-worktree", "scitex-scholar", "--expected-tip", "1" * 40]
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        (config, {"expected_tip": "1" * 40, "apply": False, "receipt_sha256": None})
    ]
    assert '"mode": "dry-run"' in result.output
